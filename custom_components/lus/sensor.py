"""Estimated LUS bill sensors, for dashboards and automations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LusConfigEntry, LusCoordinator, LusData
from .cost import BillEstimate


@dataclass(frozen=True, kw_only=True)
class LusBillDescription(SensorEntityDescription):
    """A bill sensor: which cycle it reads and which figure."""

    cycle: Callable[[LusData], BillEstimate]
    value: Callable[[BillEstimate], float | None]


SENSORS = (
    LusBillDescription(
        key="current_bill",
        translation_key="current_bill",
        cycle=lambda data: data.current,
        value=lambda bill: bill.cost,
    ),
    LusBillDescription(
        key="projected_bill",
        translation_key="projected_bill",
        cycle=lambda data: data.current,
        value=lambda bill: bill.projected,
    ),
    LusBillDescription(
        key="last_bill",
        translation_key="last_bill",
        cycle=lambda data: data.previous,
        value=lambda bill: bill.cost,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        LusBillSensor(coordinator, entry.unique_id, hass.config.currency, description)
        for description in SENSORS
    )


class LusBillSensor(CoordinatorEntity[LusCoordinator], SensorEntity):
    """Estimated charges for a billing cycle, from the bill rates in the options."""

    entity_description: LusBillDescription
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: LusCoordinator,
        account_id: str,
        currency: str,
        description: LusBillDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{account_id}_{description.key}"
        self._attr_native_unit_of_measurement = currency
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name="LUS",
            manufacturer="Lafayette Utilities System",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _bill(self) -> BillEstimate | None:
        data = self.coordinator.data
        return self.entity_description.cycle(data) if data else None

    @property
    def native_value(self) -> float | None:
        bill = self._bill()
        if bill is None:
            return None
        value = self.entity_description.value(bill)
        return round(value, 2) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        bill = self._bill()
        if bill is None:
            return None
        return {
            # The bill's service period: prior read date through current read date.
            "period_start": bill.cycle_start.date().isoformat(),
            "period_end": bill.cycle_end.date().isoformat(),
            "data_through": (
                (bill.data_through - timedelta(seconds=1)).isoformat()
                if bill.data_through
                else None
            ),
            "complete": bill.complete,
        }
