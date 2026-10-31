"""LUS (Lafayette Utilities System) usage via the Opower DSS portal."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .coordinator import LusConfigEntry, LusCoordinator

PLATFORMS = [Platform.SENSOR]


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
