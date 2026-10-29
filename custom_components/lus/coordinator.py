"""Imports LUS meter reads into Home Assistant long-term statistics."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from zoneinfo import ZoneInfo

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
    LusAuthError,
    LusClient,
    LusError,
    Meter,
    to_hourly,
)
from .const import CHUNK_DAYS, DEFAULT_BACKFILL, DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

SITE_TZ = ZoneInfo("America/Chicago")

# fuel type -> (label, unit, unit class)
_FUELS = {
    FUEL_ELECTRICITY: ("electricity", UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter.UNIT_CLASS),
    FUEL_WATER: ("water", UnitOfVolume.GALLONS, VolumeConverter.UNIT_CLASS),
    FUEL_WASTE_WATER: ("wastewater", UnitOfVolume.GALLONS, VolumeConverter.UNIT_CLASS),
}

type LusConfigEntry = ConfigEntry[LusCoordinator]


def statistic_id(meter: Meter) -> str:
    """Statistic id for a meter's consumption, e.g. lus:1000000001_electricity_consumption."""
    label = _FUELS[meter.fuel_type][0]
    return f"{DOMAIN}:{meter.utility_account_id}_{label}_consumption"


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
        try:
            await self.client.async_login()
            meters = await self.client.async_get_meters()
            for meter in meters:
                if meter.fuel_type not in _FUELS:
                    _LOGGER.debug("Skipping unsupported fuel type %s", meter.fuel_type)
                    continue
                await self._async_import_meter(meter)
        except LusAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (LusError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err

    async def _async_import_meter(self, meter: Meter) -> None:
        stat_id = statistic_id(meter)
        label, unit, unit_class = _FUELS[meter.fuel_type]

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        today = dt_util.now(SITE_TZ).date()
        if last and last.get(stat_id):
            row = last[stat_id][0]
            last_start: datetime | None = dt_util.utc_from_timestamp(row["start"])
            total = row["sum"] or 0.0
            fetch_from = last_start.astimezone(SITE_TZ).date()
        else:
            last_start = None
            total = 0.0
            fetch_from = meter.first_read or (today - DEFAULT_BACKFILL)
            _LOGGER.info("Backfilling %s from %s", stat_id, fetch_from)

        reads = []
        for start, end in _chunks(fetch_from, today, CHUNK_DAYS):
            reads.extend(await self.client.async_get_reads(meter, start, end))

        statistics = []
        for hour in to_hourly(reads, meter.reads_per_hour):
            if last_start is not None and hour.start <= last_start:
                continue
            total += hour.value
            statistics.append(
                StatisticData(start=hour.start, state=hour.value, sum=total)
            )
        if not statistics:
            _LOGGER.debug("No new complete hours for %s", stat_id)
            return

        _LOGGER.debug(
            "Adding %d hours to %s, through %s",
            len(statistics),
            stat_id,
            statistics[-1]["start"],
        )
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"LUS {label} {meter.utility_account_id}",
                source=DOMAIN,
                statistic_id=stat_id,
                unit_class=unit_class,
                unit_of_measurement=unit,
            ),
            statistics,
        )


def _chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Split an inclusive date range into inclusive chunks of at most `days` days."""
    chunks = []
    while start <= end:
        chunk_end = min(start + timedelta(days=days - 1), end)
        chunks.append((start, chunk_end))
        start = chunk_end + timedelta(days=1)
    return chunks
