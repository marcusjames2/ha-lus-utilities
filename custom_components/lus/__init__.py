"""LUS (Lafayette Utilities System) usage via the Opower DSS portal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .const import CONF_RECALCULATE_FROM, RECALCULATE_ALL
from .coordinator import LusConfigEntry, LusCoordinator
from .cost import Rates

PLATFORMS = [Platform.SENSOR]

# 0.4.0 guessed May-October for the summer months and saved it with every set of rates.
_GUESSED_SUMMER_MONTHS = "5,6,7,8,9,10"


async def async_setup_entry(hass: HomeAssistant, entry: LusConfigEntry) -> bool:
    coordinator = LusCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Statistics import must keep running even if every sensor is disabled,
    # and the coordinator only schedules refreshes while something listens.
    @callback
    def _noop() -> None:
        pass

    entry.async_on_unload(coordinator.async_add_listener(_noop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LusConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: LusConfigEntry) -> bool:
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry, options=migrate_options_1_2(entry.options), minor_version=2
        )
    if entry.minor_version < 3:
        hass.config_entries.async_update_entry(
            entry, options=migrate_options_1_3(entry.options), minor_version=3
        )
    if entry.minor_version < 4:
        hass.config_entries.async_update_entry(
            entry, options=migrate_options_1_4(entry.options), minor_version=4
        )
    return True


def migrate_options_1_2(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options for 0.4.1, which follows LUS's April-November summer months.

    Sets of rates still holding 0.4.0's guessed months move to the new default;
    months someone changed are kept. Every stored cost is recomputed on the next
    update, since the water and wastewater rules changed for all hours.
    """

    def months(rates: Mapping[str, Any]) -> dict[str, Any]:
        if rates.get("wastewater_summer_months") == _GUESSED_SUMMER_MONTHS:
            return {**rates, "wastewater_summer_months": Rates().wastewater_summer_months}
        return dict(rates)

    # 0.2.x kept one set of rates directly in the options.
    migrated = months(options)
    if migrated.get("rate_schedule") is not None:
        migrated["rate_schedule"] = [months(e) for e in migrated["rate_schedule"]]
    migrated[CONF_RECALCULATE_FROM] = RECALCULATE_ALL.isoformat()
    return migrated


def migrate_options_1_3(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options for 0.5.0, whose default water tier-1 limit is worked out from
    the meter. Saved rates carry their own limit and are kept; without them
    the limit was a fixed 20,470 gallons, so every stored cost is recomputed.
    """
    if "rate_schedule" in options or "water_tier1_gallons" in options:
        return dict(options)
    return {**options, CONF_RECALCULATE_FROM: RECALCULATE_ALL.isoformat()}


def migrate_options_1_4(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options for 0.5.1, whose costs cover hours a meter skipped. Stored costs
    left those hours out, along with the other meters' charges for them in the
    total, so every stored cost is recomputed."""
    return {**options, CONF_RECALCULATE_FROM: RECALCULATE_ALL.isoformat()}
