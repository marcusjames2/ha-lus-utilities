"""Estimates LUS charges hour by hour from the rates on a bill.

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .api import HourlyUsage

SITE_TZ = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class Rates:
    """Rates and fixed charges from an LUS bill. Defaults are the October 2026 bill."""

    electric_energy_rate: float = 0.05224
    electric_fuel_adjustment_rate: float = 0.044
    electric_customer_charge: float = 13.00
    water_tier1_rate: float = 0.00209
    water_tier2_rate: float = 0.00331
    water_tier1_gallons: float = 20470
    water_customer_charge: float = 6.99
    water_state_fee: float = 1.00
    wastewater_rate: float = 0.00747
    wastewater_customer_charge: float = 11.29
    # Billing months (by the month a cycle ends) where wastewater is not
    # charged on water used above the tier-1 limit.
    wastewater_summer_months: str = "5,6,7,8,9,10"
    garbage_charge: float = 38.04
    # Day of the month the meters are read; billing cycles start at midnight on it.
    billing_day: int = 23

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Rates:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in options.items() if k in known})

    def as_options(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def summer_months(self) -> set[int]:
        return {
            int(m) for m in self.wastewater_summer_months.replace(" ", "").split(",") if m
        }


def cycle_bounds(when: datetime, billing_day: int) -> tuple[datetime, datetime]:
    """Return the start and end of the billing cycle containing `when`."""
    local = when.astimezone(SITE_TZ)
    start = _cycle_start(local.year, local.month, billing_day)
    if local < start:
        start = _cycle_start(*_add_month(local.year, local.month, -1), billing_day)
    end = _cycle_start(*_add_month(start.year, start.month, 1), billing_day)
    return start, end


def _cycle_start(year: int, month: int, billing_day: int) -> datetime:
    return datetime(year, month, min(billing_day, 28), tzinfo=SITE_TZ)


def _add_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


class _Cycles:
    """Caches billing cycle lookups for a run of hours."""

    def __init__(self, billing_day: int) -> None:
        self._billing_day = billing_day
        self._current: tuple[datetime, datetime] | None = None

    def bounds(self, hour: datetime) -> tuple[datetime, datetime]:
        if self._current is None or not self._current[0] <= hour < self._current[1]:
            self._current = cycle_bounds(hour, self._billing_day)
        return self._current

    def hours(self, hour: datetime) -> float:
        start, end = self.bounds(hour)
        return (end - start) / timedelta(hours=1)


def electricity_costs(hours: Iterable[HourlyUsage], rates: Rates) -> list[HourlyUsage]:
    """Cost per hour: kWh at the energy and fuel adjustment rates, plus the
    customer charge spread evenly over the billing cycle."""
    cycles = _Cycles(rates.billing_day)
    per_kwh = rates.electric_energy_rate + rates.electric_fuel_adjustment_rate
    return [
        HourlyUsage(
            h.start,
            h.value * per_kwh + rates.electric_customer_charge / cycles.hours(h.start),
        )
        for h in hours
    ]


def water_costs(hours: Iterable[HourlyUsage], rates: Rates) -> list[HourlyUsage]:
    """Cost per hour of water and wastewater, both billed from the water meter.

    Gallons count toward the tier-1 limit from the start of each billing cycle,
    so `hours` should begin at a cycle start for the tiers to come out right.
    """
    cycles = _Cycles(rates.billing_day)
    summer = rates.summer_months
    fixed = (
        rates.water_customer_charge
        + rates.water_state_fee
        + rates.wastewater_customer_charge
    )
    costs = []
    cycle_start = None
    used = 0.0
    for h in hours:
        start, end = cycles.bounds(h.start)
        if start != cycle_start:
            cycle_start, used = start, 0.0
        tier1 = max(0.0, min(h.value, rates.water_tier1_gallons - used))
        tier2 = h.value - tier1
        used += h.value

        cost = tier1 * rates.water_tier1_rate + tier2 * rates.water_tier2_rate
        # A cycle that ends at midnight on the 1st belongs to the prior month.
        billed_month = (end - timedelta(seconds=1)).month
        sewer_gallons = tier1 if billed_month in summer else h.value
        cost += sewer_gallons * rates.wastewater_rate
        cost += fixed / cycles.hours(h.start)
        costs.append(HourlyUsage(h.start, cost))
    return costs


def garbage_cost(hour: datetime, rates: Rates) -> float:
    """The flat garbage charge spread evenly over the billing cycle."""
    return rates.garbage_charge / _Cycles(rates.billing_day).hours(hour)
