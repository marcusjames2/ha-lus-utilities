"""Imports LUS meter reads and estimated costs into long-term statistics."""

from __future__ import annotations

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
)
from .cost import (
    SITE_TZ,
    Rates,
    RateSchedule,
    cycle_bounds,
    electricity_costs,
    garbage_cost,
    water_costs,
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


class LusCoordinator(DataUpdateCoordinator[None]):
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

    async def _async_update_data(self) -> None:
        rates = RateSchedule.from_options(self.config_entry.options)
        # After rates change, costs from this point on are recomputed and
        # overwrite what's stored; earlier hours are left as they are.
        recalculate_from = self._recalculate_from()
        try:
            await self.client.async_login()
            meters = [
                m for m in await self.client.async_get_meters() if m.fuel_type in _FUELS
            ]
            total_id = total_cost_statistic_id(self.client.account_id)
            total_last = await self._async_last(total_id, recalculate_from)
            costs = []
            for meter in meters:
                meter_costs = await self._async_import_meter(
                    meter, rates, total_last, recalculate_from
                )
                if meter_costs is not None:
                    costs.append(meter_costs)
        except LusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (LusError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err

        if costs:
            self._add(
                total_id,
                total_last,
                total_costs(costs, rates),
                self._cost_metadata(total_id, f"LUS total cost {self.client.account_id}"),
            )
        if recalculate_from is not None:
            _LOGGER.info("Recalculated costs from %s", recalculate_from)
            options = {
                k: v
                for k, v in self.config_entry.options.items()
                if k != CONF_RECALCULATE_FROM
            }
            self.hass.config_entries.async_update_entry(self.config_entry, options=options)

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
        today = dt_util.now(SITE_TZ).date()
        resume = [self._resume_date(consumption_last, meter, today, None)]
        if cost_id:
            cost_last = await self._async_last(cost_id, recalculate_from)
            # Cost depends on usage earlier in the billing cycle, so re-read
            # from the start of the cycle being resumed.
            resume.append(self._resume_date(cost_last, meter, today, rates.billing_day))
            resume.append(self._resume_date(total_last, meter, today, rates.billing_day))
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

        costs = cost_fn(hours, rates)
        self._add(
            cost_id,
            cost_last,
            costs,
            self._cost_metadata(cost_id, f"LUS {label} cost {meter.utility_account_id}"),
        )
        return costs

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
        last: LastStat, meter: Meter, today: date, billing_day: int | None
    ) -> date:
        """First day to fetch so everything after `last` can be computed."""
        last_start, _ = last
        if last_start is None:
            return meter.first_read or (today - DEFAULT_BACKFILL)
        if billing_day is not None:
            return cycle_bounds(last_start, billing_day)[0].date()
        return last_start.astimezone(SITE_TZ).date()

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
    """Sum per-meter costs plus garbage, for hours every meter has reported."""
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
