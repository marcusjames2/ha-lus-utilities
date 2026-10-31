"""Estimates LUS charges hour by hour from the rates on a bill.

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
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


class RateSchedule:
    """Bill rates over time. Each set applies from its date until the next set's.

    A set with no date applies from the beginning of history. Bills arrive after
    their cycle closes, so a set is normally dated to the start of the cycle the
    bill covers.
    """

    def __init__(self, periods: Iterable[tuple[date | None, Rates]] = ()) -> None:
        periods = sorted(periods, key=lambda p: p[0] or date.min)
        self._periods: list[tuple[date | None, Rates]] = periods or [(None, Rates())]
        self._starts = [
            datetime(d.year, d.month, d.day, tzinfo=SITE_TZ) if d else None
            for d, _ in self._periods
        ]

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> RateSchedule:
        entries = options.get("rate_schedule")
        if entries is None:
            # 0.2.x kept one set of rates directly in the options.
            return cls([(None, Rates.from_options(options))])
        return cls(
            (
                date.fromisoformat(e["effective_from"]) if e.get("effective_from") else None,
                Rates.from_options(e),
            )
            for e in entries
        )

    def as_options(self) -> list[dict[str, Any]]:
        return [
            {"effective_from": d.isoformat() if d else None, **rates.as_options()}
            for d, rates in self._periods
        ]

    def with_rates(self, effective_from: date, rates: Rates) -> RateSchedule:
        """A copy with `rates` from `effective_from` until the next later set."""
        kept = [(d, r) for d, r in self._periods if d != effective_from]
        return RateSchedule([*kept, (effective_from, rates)])

    @property
    def periods(self) -> list[tuple[date | None, Rates]]:
        return list(self._periods)

    @property
    def latest(self) -> Rates:
        return self._periods[-1][1]

    @property
    def billing_day(self) -> int:
        return self.latest.billing_day

    def at(self, when: datetime) -> Rates:
        """The rates in effect at `when`."""
        for start, (_, rates) in zip(reversed(self._starts), reversed(self._periods)):
            if start is None or start <= when:
                return rates
        return self._periods[0][1]


def _as_schedule(rates: Rates | RateSchedule) -> RateSchedule:
    return rates if isinstance(rates, RateSchedule) else RateSchedule([(None, rates)])


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


def electricity_costs(
    hours: Iterable[HourlyUsage], rates: Rates | RateSchedule
) -> list[HourlyUsage]:
    """Cost per hour: kWh at the energy and fuel adjustment rates, plus the
    customer charge spread evenly over the billing cycle."""
    schedule = _as_schedule(rates)
    cycles = _Cycles(schedule.billing_day)
    costs = []
    for h in hours:
        r = schedule.at(h.start)
        per_kwh = r.electric_energy_rate + r.electric_fuel_adjustment_rate
        costs.append(
            HourlyUsage(
                h.start,
                h.value * per_kwh + r.electric_customer_charge / cycles.hours(h.start),
            )
        )
    return costs


def water_costs(
    hours: Iterable[HourlyUsage], rates: Rates | RateSchedule
) -> list[HourlyUsage]:
    """Cost per hour of water and wastewater, both billed from the water meter.

    Gallons count toward the tier-1 limit from the start of each billing cycle,
    so `hours` should begin at a cycle start for the tiers to come out right.
    """
    schedule = _as_schedule(rates)
    cycles = _Cycles(schedule.billing_day)
    costs = []
    cycle_start = None
    used = 0.0
    for h in hours:
        r = schedule.at(h.start)
        start, end = cycles.bounds(h.start)
        if start != cycle_start:
            cycle_start, used = start, 0.0
        tier1 = max(0.0, min(h.value, r.water_tier1_gallons - used))
        tier2 = h.value - tier1
        used += h.value

        cost = tier1 * r.water_tier1_rate + tier2 * r.water_tier2_rate
        # A cycle that ends at midnight on the 1st belongs to the prior month.
        billed_month = (end - timedelta(seconds=1)).month
        sewer_gallons = tier1 if billed_month in r.summer_months else h.value
        cost += sewer_gallons * r.wastewater_rate
        fixed = r.water_customer_charge + r.water_state_fee + r.wastewater_customer_charge
        cost += fixed / cycles.hours(h.start)
        costs.append(HourlyUsage(h.start, cost))
    return costs


def garbage_cost(hour: datetime, rates: Rates | RateSchedule) -> float:
    """The flat garbage charge spread evenly over the billing cycle."""
    schedule = _as_schedule(rates)
    return schedule.at(hour).garbage_charge / _Cycles(schedule.billing_day).hours(hour)
