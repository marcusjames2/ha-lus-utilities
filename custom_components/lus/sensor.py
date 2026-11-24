"""Estimated LUS bill and spending insight sensors, for dashboards and automations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .analytics import Comparison, Insights
from .const import DOMAIN
from .coordinator import LusConfigEntry, LusCoordinator, LusData
from .cost import BillEstimate

# How many past periods the history attributes list.
HISTORY_PERIODS = 13


@dataclass(frozen=True, kw_only=True)
class LusBillDescription(SensorEntityDescription):
    """A bill sensor: which cycle it reads and which figure."""

    cycle: Callable[[LusData], BillEstimate]
    value: Callable[[LusData], float | None]


def projected_change(data: LusData) -> float | None:
    """Return projected current bill minus the last complete bill."""
    projected = data.current.projected
    previous = data.previous
    if projected is None or previous.data_through is None or not previous.complete:
        return None
    return projected - previous.cost


SENSORS = (
    LusBillDescription(
        key="current_bill",
        translation_key="current_bill",
        cycle=lambda data: data.current,
        value=lambda data: data.current.cost,
    ),
    LusBillDescription(
        key="projected_bill",
        translation_key="projected_bill",
        cycle=lambda data: data.current,
        value=lambda data: data.current.projected,
    ),
    LusBillDescription(
        key="projected_bill_change",
        translation_key="projected_bill_change",
        cycle=lambda data: data.current,
        value=projected_change,
    ),
    LusBillDescription(
        key="last_bill",
        translation_key="last_bill",
        cycle=lambda data: data.previous,
        value=lambda data: data.previous.cost,
    ),
)


def _money(currency: str) -> str:
    return currency


def _per_kwh(currency: str) -> str:
    return f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}"


def _per_gallon(currency: str) -> str:
    return f"{currency}/{UnitOfVolume.GALLONS}"


def _percent(currency: str) -> str:
    return PERCENTAGE


@dataclass(frozen=True, kw_only=True)
class LusInsightDescription(SensorEntityDescription):
    """An insight sensor: its figure, its unit and the detail behind it."""

    value: Callable[[Insights], float | None]
    attributes: Callable[[Insights], dict[str, Any]]
    # The unit, given the currency; None for a plain count.
    unit: Callable[[str], str] | None = _money
    digits: int = 2


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _comparison(comparison: Comparison | None) -> dict[str, Any] | None:
    return comparison.as_dict() if comparison else None


def _month_attributes(insights: Insights) -> dict[str, Any]:
    month = insights.this_month
    return {
        **(month.as_dict() if month else {}),
        "months": [m.as_dict() for m in insights.months[-HISTORY_PERIODS:]],
        "billing_periods": [c.as_dict() for c in insights.cycles[-HISTORY_PERIODS:]],
    }


def _year_attributes(insights: Insights) -> dict[str, Any]:
    year = insights.this_year
    return {
        **(year.as_dict() if year else {}),
        "years": [y.as_dict() for y in insights.years],
    }


def _electricity_rate_attributes(insights: Insights) -> dict[str, Any]:
    rates = insights.rates
    if rates is None:
        return {}
    period = rates.period
    return {
        "period_start": period.start.date().isoformat(),
        "period_end": period.end.date().isoformat(),
        "complete": period.complete,
        "usage_only_rate": _rounded(rates.electricity_usage_only, 5),
        "fixed_share": _rounded(rates.electricity_fixed_share, 3),
        "kwh": round(period.kwh, 1),
        "cost": round(period.charges.electricity, 2),
        "fixed_charges": round(period.charges.electric_fixed, 2),
    }


def _water_rate_attributes(insights: Insights) -> dict[str, Any]:
    rates = insights.rates
    if rates is None:
        return {}
    period = rates.period
    charges = period.charges
    combined = rates.water_and_wastewater
    return {
        "period_start": period.start.date().isoformat(),
        "period_end": period.end.date().isoformat(),
        "complete": period.complete,
        "per_1000_gallons": _rounded(combined * 1000 if combined else None, 2),
        "water_only_rate": _rounded(rates.water, 5),
        "usage_only_rate": _rounded(rates.water_usage_only, 5),
        "gallons": round(period.gallons),
        "water_cost": round(charges.water, 2),
        "wastewater_cost": round(charges.wastewater, 2),
        "fixed_charges": round(charges.water_fixed + charges.wastewater_fixed, 2),
    }


def _anomaly_attributes(insights: Insights) -> dict[str, Any]:
    anomalies = insights.anomalies
    latest = anomalies[-1] if anomalies else None
    return {
        "latest_date": latest.day.isoformat() if latest else None,
        "latest_utility": latest.utility if latest else None,
        "latest_cause": latest.cause if latest else None,
        "latest_explanation": latest.explanation if latest else None,
        "excess_cost": round(sum(a.excess_cost for a in anomalies), 2),
        "anomalies": [a.as_dict() for a in anomalies],
    }


INSIGHT_SENSORS = (
    LusInsightDescription(
        key="month_cost",
        translation_key="month_cost",
        value=lambda i: i.this_month.total if i.this_month else None,
        attributes=_month_attributes,
    ),
    LusInsightDescription(
        key="year_cost",
        translation_key="year_cost",
        value=lambda i: i.this_year.total if i.this_year else None,
        attributes=_year_attributes,
    ),
    LusInsightDescription(
        key="electricity_rate",
        translation_key="electricity_rate",
        value=lambda i: i.rates.electricity if i.rates else None,
        attributes=_electricity_rate_attributes,
        unit=_per_kwh,
        digits=5,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LusInsightDescription(
        key="water_rate",
        translation_key="water_rate",
        value=lambda i: i.rates.water_and_wastewater if i.rates else None,
        attributes=_water_rate_attributes,
        unit=_per_gallon,
        digits=5,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LusInsightDescription(
        key="cost_change_month",
        translation_key="cost_change_month",
        value=lambda i: i.month_over_month.total.pct if i.month_over_month else None,
        attributes=lambda i: {
            **(_comparison(i.month_over_month) or {}),
            "month_to_date": _comparison(i.month_to_date),
        },
        unit=_percent,
        digits=1,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LusInsightDescription(
        key="cost_change_year",
        translation_key="cost_change_year",
        value=lambda i: i.year_over_year.total.pct if i.year_over_year else None,
        attributes=lambda i: {
            **(_comparison(i.year_over_year) or {}),
            "year_to_date": _comparison(i.year_to_date),
        },
        unit=_percent,
        digits=1,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LusInsightDescription(
        key="forecast_bill",
        translation_key="forecast_bill",
        value=lambda i: i.forecast.total if i.forecast else None,
        attributes=lambda i: i.forecast.as_dict() if i.forecast else {},
    ),
    LusInsightDescription(
        key="forecast_next_bill",
        translation_key="forecast_next_bill",
        value=lambda i: i.forecast_next.total if i.forecast_next else None,
        attributes=lambda i: i.forecast_next.as_dict() if i.forecast_next else {},
    ),
    LusInsightDescription(
        key="usage_anomalies",
        translation_key="usage_anomalies",
        value=lambda i: len(i.anomalies),
        attributes=_anomaly_attributes,
        unit=None,
        digits=0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(
                LusBillSensor(coordinator, entry.unique_id, hass.config.currency, description)
                for description in SENSORS
            ),
            LusWinterAverageSensor(coordinator, entry.unique_id),
            *(
                LusInsightSensor(
                    coordinator, entry.unique_id, hass.config.currency, description
                )
                for description in INSIGHT_SENSORS
            ),
        ]
    )


def _device_info(account_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, account_id)},
        name="LUS",
        manufacturer="Lafayette Utilities System",
        entry_type=DeviceEntryType.SERVICE,
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
        data = self.coordinator.data
        if data is None:
            return None
        value = self.entity_description.value(data)
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


class LusWinterAverageSensor(CoordinatorEntity[LusCoordinator], SensorEntity):
    """Calculated average use for the latest completed December-March winter."""

    _attr_has_entity_name = True
    _attr_translation_key = "winter_water_average"
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: LusCoordinator, account_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{account_id}_winter_water_average"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name="LUS",
            manufacturer="Lafayette Utilities System",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None or data.winter_average.gallons is None:
            return None
        return round(data.winter_average.gallons, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None:
            return None
        winter = data.winter_average
        return {
            "winter_year": winter.year,
            "water_meter_id": winter.water_meter_id,
        }


class LusInsightSensor(CoordinatorEntity[LusCoordinator], SensorEntity):
    """A spending figure worked out from the usage history and bill rates."""

    entity_description: LusInsightDescription
    _attr_has_entity_name = True
    # Period lists are detail for dashboards; keeping every copy of them in the
    # recorder would only add bulk.
    _unrecorded_attributes = frozenset(
        {"months", "billing_periods", "years", "anomalies"}
    )

    def __init__(
        self,
        coordinator: LusCoordinator,
        account_id: str,
        currency: str,
        description: LusInsightDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{account_id}_{description.key}"
        self._attr_suggested_display_precision = description.digits
        if description.unit is not None:
            self._attr_native_unit_of_measurement = description.unit(currency)
        if description.unit is _money:
            self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_device_info = _device_info(account_id)

    def _insights(self) -> Insights | None:
        data = self.coordinator.data
        return data.insights if data else None

    @property
    def native_value(self) -> float | None:
        insights = self._insights()
        if insights is None:
            return None
        return _rounded(
            self.entity_description.value(insights), self.entity_description.digits
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        insights = self._insights()
        if insights is None:
            return None
        through = insights.data_through
        return {
            **self.entity_description.attributes(insights),
            "data_through": (
                (through - timedelta(seconds=1)).isoformat() if through else None
            ),
        }
