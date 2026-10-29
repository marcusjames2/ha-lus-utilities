"""LUS (Lafayette Utilities System) usage via the Opower DSS portal."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback

from .coordinator import LusConfigEntry, LusCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: LusConfigEntry) -> bool:
    coordinator = LusCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # The integration has no entities, so nothing else listens to the
    # coordinator. A no-op listener keeps its scheduled refreshes running.
    @callback
    def _noop() -> None:
        pass

    entry.async_on_unload(coordinator.async_add_listener(_noop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LusConfigEntry) -> bool:
    return True
