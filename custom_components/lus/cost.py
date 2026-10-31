"""Estimates LUS charges hour by hour from the rates on a bill.

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
import math
from typing import Any
from zoneinfo import ZoneInfo

from .api import HourlyUsage, next_hour

SITE_TZ = ZoneInfo("America/Chicago")

# Schedule S1: summer wastewater is never charged on less than this share of
# the water actually used.
WASTEWATER_MIN_SHARE = 0.75

# Bills whose average water use sets the next summer's tier-1 limit (W1, S1).
WINTER_MONTHS = frozenset({12, 1, 2, 3})

# A winter bill still counts toward the average with up to this much of its
# cycle missing from the interval reads; its use is scaled up to the whole cycle.
WINTER_COVERAGE_SLACK = timedelta(days=1)

# Day of the month cycles start on when neither the options nor the portal's
# bills say: the integration's original default.
DEFAULT_CYCLE_DAY = 23


@dataclass(frozen=True)
class Rates:
    """Rates and fixed charges from an LUS bill. Defaults are the October 2026 bill,
    apart from the tier-1 limit."""

    electric_energy_rate: float = 0.05224
    electric_fuel_adjustment_rate: float = 0.044
    electric_customer_charge: float = 13.00
    water_tier1_rate: float = 0.00209
    water_tier2_rate: float = 0.00331
    # The household's average use over the previous December-March bills,
    # which LUS recalculates each year. 0 works it out from the water meter.
    water_tier1_gallons: float = 0
    water_customer_charge: float = 6.99
    water_state_fee: float = 1.00
    wastewater_rate: float = 0.00747
    wastewater_customer_charge: float = 11.29
    # Billing months (by the month a cycle ends) under the summer rules of LUS
    # schedules W1 and S1: water above the tier-1 limit is charged at the
    # tier-2 rate, and wastewater is capped at the tier-1 limit. Other months
    # charge every gallon at the tier-1 rate and in full for wastewater.
    wastewater_summer_months: str = "4,5,6,7,8,9,10,11"
    garbage_charge: float = 38.04
    # Day of the month billing cycles start, at midnight. 0 takes each meter's
    # own bill periods from the portal (see BillingCycles).
    billing_day: int = 0

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

    def __init__(
        self,
        periods: Iterable[tuple[date | None, Rates]] = (),
        bill_cycles: BillingCycles | None = None,
    ) -> None:
        periods = sorted(periods, key=lambda p: p[0] or date.min)
        self._periods: list[tuple[date | None, Rates]] = periods or [(None, Rates())]
        self._starts = [
            datetime(d.year, d.month, d.day, tzinfo=SITE_TZ) if d else None
            for d, _ in self._periods
        ]
        self._bill_cycles = bill_cycles

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
        return RateSchedule([*kept, (effective_from, rates)], self._bill_cycles)

    def with_bill_cycles(self, bill_cycles: BillingCycles | None) -> RateSchedule:
        """A copy whose cycles follow a meter's bills, unless the rates name a day."""
        return RateSchedule(self._periods, bill_cycles)

    @property
    def periods(self) -> list[tuple[date | None, Rates]]:
        return list(self._periods)

    @property
    def latest(self) -> Rates:
        return self._periods[-1][1]

    @property
    def billing_day(self) -> int:
        return self.latest.billing_day

    @property
    def cycles(self) -> BillingCycles:
        """Where billing cycles start: on the day the rates name, or else on the
        meter's own bill dates."""
        if self.billing_day:
            return BillingCycles(day=self.billing_day)
        return self._bill_cycles or BillingCycles()

    def at(self, when: datetime) -> Rates:
        """The rates in effect at `when`."""
        for start, (_, rates) in zip(reversed(self._starts), reversed(self._periods)):
            if start is None or start <= when:
                return rates
        return self._periods[0][1]


def as_schedule(rates: Rates | RateSchedule) -> RateSchedule:
    return rates if isinstance(rates, RateSchedule) else RateSchedule([(None, rates)])


class BillingCycles:
    """Where a meter's billing cycles start and end.

    Within the bill periods the portal reports, cycles are exactly those
    periods: LUS reads meters on a different day some months, so no single day
    fits. Before the first period and after the last, cycles fall on that
    period's day every month, so the open cycle is taken to end a month after
    the last bill. With no periods, they fall on `day` every month.
    """

    def __init__(
        self,
        periods: Iterable[tuple[datetime, datetime]] = (),
        day: int = DEFAULT_CYCLE_DAY,
    ) -> None:
        self._bounds = sorted({t.astimezone(SITE_TZ) for period in periods for t in period})
        self._day = day

    @property
    def last_bill_end(self) -> datetime | None:
        """End of the newest bill period, if there are any."""
        return self._bounds[-1] if self._bounds else None

    def bounds(self, when: datetime) -> tuple[datetime, datetime]:
        """Return the start and end of the billing cycle containing `when`."""
        known = self._bounds
        if len(known) >= 2 and known[0] <= when < known[-1]:
            i = bisect_right(known, when) - 1
            return known[i], known[i + 1]
        if known and when >= known[-1]:
            start, end = _monthly_bounds(when, known[-1].day)
            return max(start, known[-1]), end
        if known:
            start, end = _monthly_bounds(when, known[0].day)
            return start, min(end, known[0])
        return _monthly_bounds(when, self._day)


def cycle_bounds(
    when: datetime, cycles: int | BillingCycles
) -> tuple[datetime, datetime]:
    """Return the start and end of the billing cycle containing `when`, for
    cycles starting on a day of the month or following a meter's bills."""
    if isinstance(cycles, BillingCycles):
        return cycles.bounds(when)
    return _monthly_bounds(when, cycles)


def _monthly_bounds(when: datetime, billing_day: int) -> tuple[datetime, datetime]:
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

    def __init__(self, cycles: int | BillingCycles) -> None:
        self._cycles = cycles
        self._current: tuple[datetime, datetime] | None = None

    def bounds(self, hour: datetime) -> tuple[datetime, datetime]:
        if self._current is None or not self._current[0] <= hour < self._current[1]:
            self._current = cycle_bounds(hour, self._cycles)
        return self._current

    def hours(self, hour: datetime) -> float:
        start, end = self.bounds(hour)
        return (end - start) / timedelta(hours=1)


def fill_gaps(hours: Iterable[HourlyUsage]) -> list[HourlyUsage]:
    """`hours` with a zero-use hour for each one missing between them.

    The meter data can skip hours, but the bill's fixed charges and the other
    meters' charges don't stop for them, so costs are worked out over every hour.
    """
    filled: list[HourlyUsage] = []
    for h in hours:
        if filled:
            t = next_hour(filled[-1].start)
            while t < h.start:
                filled.append(HourlyUsage(t.astimezone(SITE_TZ), 0.0))
                t = next_hour(t)
        filled.append(h)
    return filled


def electricity_costs(
    hours: Iterable[HourlyUsage], rates: Rates | RateSchedule
) -> list[HourlyUsage]:
    """Cost per hour: kWh at the energy and fuel adjustment rates, plus the
    customer charge spread evenly over the billing cycle."""
    schedule = as_schedule(rates)
    cycles = _Cycles(schedule.cycles)
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
    hours: Iterable[HourlyUsage],
    rates: Rates | RateSchedule,
    winters: Mapping[int, float] | None = None,
) -> list[HourlyUsage]:
    """Cost per hour of water and wastewater, both billed from the water meter.

    Gallons count toward the tier-1 limit from the start of each billing cycle,
    so `hours` should begin at a cycle start for the tiers to come out right.
    Where the rates leave the limit at 0, it's the average in `winters` for the
    winter before the bill (see `winter_average`); with none, there are no tiers.
    """
    schedule = as_schedule(rates)
    cycles = _Cycles(schedule.cycles)
    costs = []
    cycle_start = None
    used = 0.0
    for h in hours:
        r = schedule.at(h.start)
        start, end = cycles.bounds(h.start)
        if start != cycle_start:
            cycle_start, used = start, 0.0
        before, used = used, used + h.value

        year, billed_month = _billed_month(end)
        if billed_month in r.summer_months:
            limit = r.water_tier1_gallons or (winters or {}).get(
                _limit_winter(year, billed_month), math.inf
            )
            tier1 = max(0.0, min(h.value, limit - before))
            sewer_gallons = _summer_sewer_gallons(used, limit) - _summer_sewer_gallons(
                before, limit
            )
        else:
            tier1 = sewer_gallons = h.value
        tier2 = h.value - tier1

        cost = tier1 * r.water_tier1_rate + tier2 * r.water_tier2_rate
        cost += sewer_gallons * r.wastewater_rate
        fixed = r.water_customer_charge + r.water_state_fee + r.wastewater_customer_charge
        cost += fixed / cycles.hours(h.start)
        costs.append(HourlyUsage(h.start, cost))
    return costs


def _summer_sewer_gallons(used: float, limit: float) -> float:
    """Wastewater gallons charged for `used` gallons so far in a summer cycle:
    the tier-1 limit or actual use, whichever is less, but never less than
    WASTEWATER_MIN_SHARE of actual use."""
    return max(min(used, limit), WASTEWATER_MIN_SHARE * used)


def _billed_month(cycle_end: datetime) -> tuple[int, int]:
    """(year, month) of the bill for the cycle ending at `cycle_end`.

    A cycle that ends at midnight on the 1st belongs to the prior month.
    """
    last = cycle_end - timedelta(seconds=1)
    return last.year, last.month


def _limit_winter(year: int, month: int) -> int | None:
    """The winter, by the year of its March bill, whose average sets the tier-1
    limit of the bill for `month` of `year`: that year's, for April-November.
    None for a December-March bill, whose winter isn't over."""
    return None if month in WINTER_MONTHS else year


def winter_cycles(
    winter: int, cycles: int | BillingCycles
) -> list[tuple[datetime, datetime]]:
    """The cycles of the December-March bills of the winter whose March bill
    falls in `winter`."""
    months = ((winter - 1, 12), (winter, 1), (winter, 2), (winter, 3))
    # The cycle holding the 1st of a month is the one billed in that month.
    return [cycle_bounds(datetime(y, m, 1, tzinfo=SITE_TZ), cycles) for y, m in months]


def winter_average(
    hours: Iterable[HourlyUsage], cycles: list[tuple[datetime, datetime]]
) -> tuple[float | None, bool]:
    """Average water use per bill over `cycles`, and whether `hours` covers
    every one of them.

    A cycle missing up to WINTER_COVERAGE_SLACK of its hours counts, its use
    scaled up to the whole cycle: LUS bills from the meter's register, which
    gaps in the interval reads don't lower. The average is over the cycles
    covered, as LUS averages "such portion of said winter months as may have
    been metered"; None if there are none.
    """
    # Hours are counted by instant, so DST changes don't throw the count off.
    stamps: dict[datetime, set[float]] = {start: set() for start, _ in cycles}
    gallons = dict.fromkeys(stamps, 0.0)
    for h in hours:
        for start, end in cycles:
            if start <= h.start < end:
                stamps[start].add(h.start.timestamp())
                gallons[start] += h.value
                break

    covered = []
    slack = WINTER_COVERAGE_SLACK / timedelta(hours=1)
    for start, end in cycles:
        whole = (end.timestamp() - start.timestamp()) / 3600
        have = len(stamps[start])
        if have and whole - have <= slack:
            covered.append(gallons[start] * whole / have)
    average = sum(covered) / len(covered) if covered else None
    return average, len(covered) == len(cycles)


def winters_needed(
    hours: Iterable[HourlyUsage], rates: Rates | RateSchedule
) -> dict[int, datetime]:
    """Winters whose average sets the tier-1 limit of a summer bill in `hours`
    whose rates leave the limit at 0, each with the first hour that needs it."""
    schedule = as_schedule(rates)
    cycles = _Cycles(schedule.cycles)
    needed: dict[int, datetime] = {}
    for h in hours:
        r = schedule.at(h.start)
        year, month = _billed_month(cycles.bounds(h.start)[1])
        winter = _limit_winter(year, month)
        if month in r.summer_months and not r.water_tier1_gallons and winter is not None:
            needed[winter] = min(needed.get(winter, h.start), h.start)
    return needed


def garbage_cost(hour: datetime, rates: Rates | RateSchedule) -> float:
    """The flat garbage charge spread evenly over the billing cycle."""
    schedule = as_schedule(rates)
    return schedule.at(hour).garbage_charge / _Cycles(schedule.cycles).hours(hour)


@dataclass(frozen=True)
class BillEstimate:
    """Estimated charges for one billing cycle, from the hours imported so far."""

    cycle_start: datetime
    cycle_end: datetime
    cost: float
    # End of the newest hour counted; None when the cycle has no data yet.
    data_through: datetime | None

    @property
    def complete(self) -> bool:
        return self.data_through is not None and self.data_through >= self.cycle_end

    @property
    def projected(self) -> float | None:
        """The cost so far scaled to the whole cycle, at the same hourly rate."""
        if self.data_through is None:
            return None
        covered = (self.data_through - self.cycle_start) / timedelta(hours=1)
        whole = (self.cycle_end - self.cycle_start) / timedelta(hours=1)
        return self.cost * whole / covered if covered > 0 else None


def bill_estimates(
    total_hours: Iterable[HourlyUsage], cycles: int | BillingCycles, now: datetime
) -> tuple[BillEstimate, BillEstimate]:
    """The current cycle (the one containing `now`) and the one before it.

    `total_hours` must cover both cycles from their start for the sums to be whole.
    """
    current_start, current_end = cycle_bounds(now, cycles)
    previous_start, _ = cycle_bounds(current_start - timedelta(seconds=1), cycles)

    hours = list(total_hours)

    def estimate(start: datetime, end: datetime) -> BillEstimate:
        inside = [h for h in hours if start <= h.start < end]
        through = max(h.start for h in inside) + timedelta(hours=1) if inside else None
        return BillEstimate(start, end, sum(h.value for h in inside), through)

    return (
        estimate(current_start, current_end),
        estimate(previous_start, current_start),
    )
