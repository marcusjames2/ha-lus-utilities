"""Imports LUS meter reads and estimated costs into long-term statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import (
    FUEL_ELECTRICITY,
    FUEL_WASTE_WATER,
    FUEL_WATER,
    HourlyUsage,
    LusAuthError,
    LusClient,
    LusError,
    Meter,
    to_hourly,
)
from .const import (
    CHUNK_DAYS,
    CONF_RECALCULATE_FROM,
    DEFAULT_BACKFILL,
    DOMAIN,
    RECALCULATE_LOOKBACK,
    UPDATE_INTERVAL,
    WINTER_SETTLE,
)
from .cost import (
    SITE_TZ,
    BillEstimate,
    BillingCycles,
    Rates,
    RateSchedule,
    as_schedule,
    bill_estimates,
    cycle_bounds,
    electricity_costs,
    fill_gaps,
    garbage_cost,
    water_costs,
    winter_average,
    winter_cycles,
    winters_needed,
)

_LOGGER = logging.getLogger(__name__)

# fuel type -> (label, unit, unit class)
_FUELS = {
    FUEL_ELECTRICITY: ("electricity", UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS),
    FUEL_WATER: ("water", UnitOfVolume.GALLONS, VolumeConverter.UNIT_CLASS),
    FUEL_WASTE_WATER: ("wastewater", UnitOfVolume.GALLONS, VolumeConverter.UNIT_CLASS),
}

# Fuels that get a cost statistic. Wastewater is billed from the water meter's
# gallons, so its charges are part of the water cost.
_COST_FUNCTIONS = {
    FUEL_ELECTRICITY: electricity_costs,
    FUEL_WATER: water_costs,
}

type LusConfigEntry = ConfigEntry[LusCoordinator]


@dataclass(frozen=True)
class LusData:
    """Estimated bills for the sensors."""

    account_id: str
    current: BillEstimate
    previous: BillEstimate

# (start of the last stored hour, its running sum); (None, 0.0) if none stored.
type LastStat = tuple[datetime | None, float]


def statistic_id(meter: Meter) -> str:
    """Statistic id for a meter's consumption, e.g. lus:1000000001_electricity_consumption."""
    label = _FUELS[meter.fuel_type][0]
    return f"{DOMAIN}:{meter.utility_account_id}_{label}_consumption"


def cost_statistic_id(meter: Meter) -> str:
    """Statistic id for a meter's estimated cost, e.g. lus:1000000001_electricity_cost."""
    label = _FUELS[meter.fuel_type][0]
    return f"{DOMAIN}:{meter.utility_account_id}_{label}_cost"


def total_cost_statistic_id(account_id: str) -> str:
    """Statistic id for the account's estimated total bill."""
    return f"{DOMAIN}:{account_id}_total_cost"


class LusCoordinator(DataUpdateCoordinator[LusData | None]):
    """Pulls new reads every few hours and appends them to statistics."""

    config_entry: LusConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LusConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = LusClient(
            async_create_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
        )
        # (meter uuid, winter, its bills' cycles) -> average gallons per bill, or
        # None if the meter has no reads for it; kept once every bill is covered.
        self._winters: dict[tuple[str, int, tuple], float | None] = {}
        # meter uuid -> end of the newest bill period seen in a completed update.
        self._bill_ends: dict[str, datetime] = {}
        # The account's billing cycles as of the last update, for the options flow.
        self.cycles: BillingCycles | None = None

    async def _async_update_data(self) -> LusData | None:
        rates = RateSchedule.from_options(self.config_entry.options)
        # After rates change, costs from this point on are recomputed and
        # overwrite what's stored; earlier hours are left as they are.
        requested = self._recalculate_from()
        recalculate_from = requested
        try:
            await self.client.async_login()
            meters = [
                m for m in await self.client.async_get_meters() if m.fuel_type in _FUELS
            ]
            # Each billed meter's cycles follow its own bills; the bill sensors
            # follow the electric meter's.
            billed = [m for m in meters if m.fuel_type in _COST_FUNCTIONS]
            meter_rates: dict[str, RateSchedule] = {}
            bill_ends: dict[str, datetime] = {}
            for meter in billed:
                meter_rates[meter.uuid], end, redo = await self._async_meter_rates(
                    meter, rates
                )
                if end is not None:
                    bill_ends[meter.uuid] = end
                if redo is not None:
                    recalculate_from = min(recalculate_from or redo, redo)
            account = next((m for m in billed if m.fuel_type == FUEL_ELECTRICITY), None)
            account = account or (billed[0] if billed else None)
            account_rates = meter_rates[account.uuid] if account else rates

            total_id = total_cost_statistic_id(self.client.account_id)
            total_last = await self._async_last(total_id, recalculate_from)
            costs = []
            for meter in meters:
                meter_costs = await self._async_import_meter(
                    meter, meter_rates.get(meter.uuid, rates), total_last, recalculate_from
                )
                if meter_costs is not None:
                    costs.append(meter_costs)
        except LusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (LusError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err

        data = None
        if costs:
            totals = total_costs(costs, account_rates)
            self._add(
                total_id,
                total_last,
                totals,
                self._cost_metadata(total_id, f"LUS total cost {self.client.account_id}"),
            )
            current, previous = bill_estimates(
                totals, account_rates.cycles, dt_util.now(SITE_TZ)
            )
            data = LusData(self.client.account_id, current, previous)
        self.cycles = account_rates.cycles
        self._bill_ends.update(bill_ends)
        if requested is not None:
            _LOGGER.info("Recalculated costs from %s", requested)
            options = {
                k: v
                for k, v in self.config_entry.options.items()
                if k != CONF_RECALCULATE_FROM
            }
            self.hass.config_entries.async_update_entry(self.config_entry, options=options)
        return data

    async def _async_meter_rates(
        self, meter: Meter, rates: RateSchedule
    ) -> tuple[RateSchedule, datetime | None, datetime | None]:
        """The rates with the meter's own bill periods as its cycles, the end of
        its newest bill, and where to recalculate costs from because of it.

        A cycle still open is costed with a guessed end, a month after the last
        bill. Once a newer bill closes it, or on the first look since starting,
        that cycle is costed again from its start with the bill's real dates.
        """
        if rates.billing_day:
            # A day set in the options wins over the portal's dates.
            return rates, None, None
        cycles = BillingCycles(await self.client.async_get_bill_periods(meter))
        end = cycles.last_bill_end
        if end is None or self._bill_ends.get(meter.uuid) == end:
            return rates.with_bill_cycles(cycles), end, None
        closed_start, _ = cycles.bounds(end - timedelta(seconds=1))
        return rates.with_bill_cycles(cycles), end, closed_start

    def _recalculate_from(self) -> datetime | None:
        value = self.config_entry.options.get(CONF_RECALCULATE_FROM)
        if not value:
            return None
        day = date.fromisoformat(value)
        return datetime(day.year, day.month, day.day, tzinfo=SITE_TZ)

    async def _async_import_meter(
        self,
        meter: Meter,
        rates: Rates | RateSchedule,
        total_last: LastStat,
        recalculate_from: datetime | None = None,
    ) -> list[HourlyUsage] | None:
        """Import consumption and, for billed meters, cost. Returns the cost hours."""
        consumption_id = statistic_id(meter)
        cost_fn = _COST_FUNCTIONS.get(meter.fuel_type)
        cost_id = cost_statistic_id(meter) if cost_fn else None

        consumption_last = await self._async_last(consumption_id)
        cycles = as_schedule(rates).cycles
        today = dt_util.now(SITE_TZ).date()
        resume = [self._resume_date(consumption_last, meter, today, None)]
        if cost_id:
            cost_last = await self._async_last(cost_id, recalculate_from)
            # Cost depends on usage earlier in the billing cycle, so re-read
            # from the start of the cycle being resumed.
            resume.append(self._resume_date(cost_last, meter, today, cycles))
            # The bill sensors need the previous cycle's total cost too.
            resume.append(
                self._resume_date(
                    total_last, meter, today, cycles, previous_cycle=True
                )
            )
        fetch_from = min(resume)

        reads = []
        for start, end in _chunks(fetch_from, today, CHUNK_DAYS):
            reads.extend(await self.client.async_get_reads(meter, start, end))
        hours = to_hourly(reads, meter.reads_per_hour)

        label, unit, unit_class = _FUELS[meter.fuel_type]
        self._add(
            consumption_id,
            consumption_last,
            hours,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"LUS {label} {meter.utility_account_id}",
                source=DOMAIN,
                statistic_id=consumption_id,
                unit_class=unit_class,
                unit_of_measurement=unit,
            ),
        )
        if not cost_id:
            return None

        cost_hours = fill_gaps(hours)
        if meter.fuel_type == FUEL_WATER:
            winters, hold_from = await self._async_winter_averages(
                meter, cost_hours, rates, today
            )
            costs = water_costs(cost_hours, rates, winters)
            if hold_from is not None:
                # Store no cost that a winter still coming in could change.
                costs = [c for c in costs if c.start < hold_from]
        else:
            costs = cost_fn(cost_hours, rates)
        self._add(
            cost_id,
            cost_last,
            costs,
            self._cost_metadata(cost_id, f"LUS {label} cost {meter.utility_account_id}"),
        )
        return costs

    async def _async_winter_averages(
        self,
        meter: Meter,
        hours: list[HourlyUsage],
        rates: Rates | RateSchedule,
        today: date,
    ) -> tuple[dict[int, float], datetime | None]:
        """December-March averages for the summer bills in `hours` that take
        their tier-1 limit from the meter, and the first hour whose cost has to
        wait for a winter's reads to come in (None if none does)."""
        cycles = as_schedule(rates).cycles
        averages = {}
        hold_from = None
        for winter, first_hour in winters_needed(hours, rates).items():
            bills = tuple(winter_cycles(winter, cycles))
            key = (meter.uuid, winter, bills)
            if key in self._winters:
                average = self._winters[key]
            else:
                average, complete = await self._async_read_winter(meter, bills, today)
                winter_end = bills[-1][1]
                if complete:
                    # Anything less is read again on the next update.
                    self._winters[key] = average
                    _LOGGER.info(
                        "Winter %s average for meter %s: %s gallons per bill",
                        winter,
                        meter.utility_account_id,
                        "no data" if average is None else round(average),
                    )
                elif today - winter_end.date() <= WINTER_SETTLE:
                    # The last winter bill's reads can lag a few days.
                    hold_from = first_hour if hold_from is None else min(hold_from, first_hour)
                    continue
            if average is not None:
                averages[winter] = average
        return averages, hold_from

    async def _async_read_winter(
        self, meter: Meter, bills: tuple[tuple[datetime, datetime], ...], today: date
    ) -> tuple[float | None, bool]:
        """The average over the winter `bills` from the portal, and whether the
        reads cover every one metered from the start of its cycle."""
        cycles = [
            (start, end)
            for start, end in bills
            if meter.first_read is None or start.date() >= meter.first_read
        ]
        if not cycles:
            return None, True
        first = cycles[0][0].date()
        last = min((cycles[-1][1] - timedelta(days=1)).date(), today)
        reads = []
        for chunk_start, chunk_end in _chunks(first, last, CHUNK_DAYS):
            reads.extend(await self.client.async_get_reads(meter, chunk_start, chunk_end))
        # A past window, so take every hour as it is.
        return winter_average(to_hourly(reads, meter.reads_per_hour, settle=timedelta(0)), cycles)

    async def _async_last(self, stat_id: str, before: datetime | None = None) -> LastStat:
        """The last stored hour, or the last one before `before` when given.

        With `before`, nothing found in the lookback window means everything is
        recomputed from the start of history.
        """
        if before is None:
            rows = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
            )
        else:
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                before - RECALCULATE_LOOKBACK,
                before,
                {stat_id},
                "hour",
                None,
                {"sum"},
            )
        if not rows or not rows.get(stat_id):
            return None, 0.0
        row = rows[stat_id][-1]
        return dt_util.utc_from_timestamp(row["start"]), row["sum"] or 0.0

    @staticmethod
    def _resume_date(
        last: LastStat,
        meter: Meter,
        today: date,
        cycles: int | BillingCycles | None,
        previous_cycle: bool = False,
    ) -> date:
        """First day to fetch so everything after `last` can be computed.

        With billing cycles, that's the start of the cycle `last` is in, or of
        the cycle before it when `previous_cycle` is set.
        """
        last_start, _ = last
        if last_start is None:
            return meter.first_read or (today - DEFAULT_BACKFILL)
        if cycles is None:
            return last_start.astimezone(SITE_TZ).date()
        start = cycle_bounds(last_start, cycles)[0]
        if previous_cycle:
            start = cycle_bounds(start - timedelta(seconds=1), cycles)[0]
        return start.date()

    def _cost_metadata(self, stat_id: str, name: str) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency,
        )

    def _add(
        self,
        stat_id: str,
        last: LastStat,
        hours: list[HourlyUsage],
        metadata: StatisticMetaData,
    ) -> None:
        """Append the hours after the last stored one, continuing its running sum."""
        last_start, total = last
        statistics = []
        for hour in hours:
            if last_start is not None and hour.start <= last_start:
                continue
            total += hour.value
            statistics.append(StatisticData(start=hour.start, state=hour.value, sum=total))
        if not statistics:
            _LOGGER.debug("No new complete hours for %s", stat_id)
            return
        _LOGGER.debug(
            "Adding %d hours to %s, through %s",
            len(statistics),
            stat_id,
            statistics[-1]["start"],
        )
        async_add_external_statistics(self.hass, metadata, statistics)


def total_costs(
    costs: list[list[HourlyUsage]], rates: Rates | RateSchedule
) -> list[HourlyUsage]:
    """Sum per-meter costs plus garbage, through the newest hour every meter has
    reported (each meter's costs cover the hours it skipped; see `fill_gaps`)."""
    by_meter = [{h.start: h.value for h in meter_costs} for meter_costs in costs]
    common = set(by_meter[0]).intersection(*by_meter[1:])
    return [
        HourlyUsage(hour, sum(m[hour] for m in by_meter) + garbage_cost(hour, rates))
        for hour in sorted(common)
    ]


def _chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Split an inclusive date range into inclusive chunks of at most `days` days."""
    chunks = []
    while start <= end:
        chunk_end = min(start + timedelta(days=days - 1), end)
        chunks.append((start, chunk_end))
        start = chunk_end + timedelta(days=1)
    return chunks
