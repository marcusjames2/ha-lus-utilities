"""Spending analytics built on the bill cost model.

Turns hourly usage into a ledger of itemised charges, then answers the
questions a bill doesn't: what each utility costs per month and per year, what
a kWh or a gallon really costs once fixed charges are included, how periods
compare, what the next bills are likely to be, and which days were unusual.

Kept free of Home Assistant imports so it can be tested on its own. Every
formula used here is written out, with a worked example, in docs/MATH.md.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from math import inf, sqrt
from statistics import median
from typing import Any

from .api import FUEL_ELECTRICITY, FUEL_WATER, HourlyUsage
from .cost import (
    SITE_TZ,
    BillingCycles,
    Rates,
    RateSchedule,
    _billed_month,
    _Cycles,
    _limit_winter,
    _summer_sewer_gallons,
    as_schedule,
    cycle_bounds,
    fill_gaps,
    winter_average,
    winter_cycles,
    winters_needed,
)

ELECTRICITY = "electricity"
WATER = "water"
WASTEWATER = "wastewater"
GARBAGE = "garbage"

# A period counts as complete when it has ended and at least this share of its
# hours have data from every billed meter.
COMPLETE_COVERAGE = 0.95

# A day needs this many hours of data to be used as a daily total (23 so the
# short spring daylight-saving day still counts).
FULL_DAY_HOURS = 23

# Forecast: how many trailing days set the daily usage level, the half-life of
# their weights, and the z-score of the reported range (1.2816 = 80%).
LEVEL_WINDOW_DAYS = 28
LEVEL_HALF_LIFE_DAYS = 7.0
RANGE_Z = 1.2816
# Year-over-year growth applied to last year's usage is kept within these.
GROWTH_LIMITS = (0.5, 1.5)
GROWTH_CYCLES = 3
# The seasonal drift applied to the rest of the current cycle is kept within
# these, and needs this share of last year's matching days to be whole.
DRIFT_LIMITS = (0.5, 2.0)
DRIFT_MIN_COVERAGE = 0.8

# Anomalies: trailing baseline length, the least baseline days needed, the
# robust z-score that flags a day, and floors that keep trivial days out.
BASELINE_DAYS = 28
BASELINE_MIN_DAYS = 14
ANOMALY_Z = 3.5
ANOMALY_MIN_RATIO = 1.25
ANOMALY_MIN_EXCESS = {ELECTRICITY: 5.0, WATER: 100.0}
# Scale of the noise floor relative to the baseline median, so a very steady
# baseline (MAD near zero) doesn't flag ordinary variation.
ANOMALY_SIGMA_FLOOR = 0.10
MAD_TO_SIGMA = 1.4826
# Water running in every hour of this many consecutive hours suggests a leak.
# An hour counts as running from this many gallons, so meter rounding doesn't.
CONTINUOUS_FLOW_HOURS = 24
CONTINUOUS_FLOW_MIN_GALLONS = 0.5
ANOMALY_REPORT_DAYS = 30


def elapsed_hours(start: datetime, end: datetime) -> float:
    """Real hours from `start` to `end`.

    Subtracting two times in the same zone gives the wall-clock difference,
    which is an hour out across a daylight-saving change, so both are taken to
    UTC first.
    """
    return (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)) / timedelta(
        hours=1
    )


def pct_change(current: float, previous: float) -> float | None:
    """Percentage change from `previous` to `current`; None when undefined."""
    if previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


@dataclass(frozen=True)
class Charges:
    """Itemised charges, in the bill's currency."""

    electric_energy: float = 0.0
    electric_fuel: float = 0.0
    electric_fixed: float = 0.0
    water_volume: float = 0.0
    water_fixed: float = 0.0
    wastewater_volume: float = 0.0
    wastewater_fixed: float = 0.0
    garbage: float = 0.0

    @classmethod
    def sum(cls, items: Iterable[Charges]) -> Charges:
        totals = [0.0] * len(_CHARGE_FIELDS)
        for item in items:
            for i, name in enumerate(_CHARGE_FIELDS):
                totals[i] += getattr(item, name)
        return cls(*totals)

    def __add__(self, other: Charges) -> Charges:
        return Charges.sum((self, other))

    @property
    def electricity(self) -> float:
        return self.electric_energy + self.electric_fuel + self.electric_fixed

    @property
    def water(self) -> float:
        return self.water_volume + self.water_fixed

    @property
    def wastewater(self) -> float:
        return self.wastewater_volume + self.wastewater_fixed

    @property
    def fixed(self) -> float:
        """Charges that don't depend on usage."""
        return self.electric_fixed + self.water_fixed + self.wastewater_fixed + self.garbage

    @property
    def variable(self) -> float:
        """Charges that scale with usage."""
        return (
            self.electric_energy
            + self.electric_fuel
            + self.water_volume
            + self.wastewater_volume
        )

    @property
    def total(self) -> float:
        return self.electricity + self.water + self.wastewater + self.garbage

    def by_utility(self) -> dict[str, float]:
        return {
            ELECTRICITY: self.electricity,
            WATER: self.water,
            WASTEWATER: self.wastewater,
            GARBAGE: self.garbage,
        }


_CHARGE_FIELDS = tuple(f.name for f in fields(Charges))


def electricity_charges(
    hours: Iterable[HourlyUsage], rates: Rates | RateSchedule
) -> list[Charges]:
    """The parts of `cost.electricity_costs`, hour by hour."""
    schedule = as_schedule(rates)
    cycles = _Cycles(schedule.cycles)
    charges = []
    for h in hours:
        r = schedule.at(h.start)
        charges.append(
            Charges(
                electric_energy=h.value * r.electric_energy_rate,
                electric_fuel=h.value * r.electric_fuel_adjustment_rate,
                electric_fixed=r.electric_customer_charge / cycles.hours(h.start),
            )
        )
    return charges


def water_charges(
    hours: Iterable[HourlyUsage],
    rates: Rates | RateSchedule,
    winters: Mapping[int, float] | None = None,
) -> list[Charges]:
    """The parts of `cost.water_costs`, hour by hour.

    As there, `hours` must be in order and should begin at a cycle start, and
    `winters` gives the tier-1 limit where the rates leave it at 0.
    """
    schedule = as_schedule(rates)
    cycles = _Cycles(schedule.cycles)
    charges = []
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
                _limit_winter(year, billed_month), inf
            )
            tier1 = max(0.0, min(h.value, limit - before))
            sewer_gallons = _summer_sewer_gallons(used, limit) - _summer_sewer_gallons(
                before, limit
            )
        else:
            tier1 = sewer_gallons = h.value
        tier2 = h.value - tier1
        cycle_hours = cycles.hours(h.start)
        charges.append(
            Charges(
                water_volume=tier1 * r.water_tier1_rate + tier2 * r.water_tier2_rate,
                water_fixed=(r.water_customer_charge + r.water_state_fee) / cycle_hours,
                wastewater_volume=sewer_gallons * r.wastewater_rate,
                wastewater_fixed=r.wastewater_customer_charge / cycle_hours,
            )
        )
    return charges


def winter_averages(
    hours: Sequence[HourlyUsage], rates: Rates | RateSchedule
) -> dict[int, float]:
    """The December-March averages that set the tier-1 limit of the summer
    bills in `hours`, worked out from `hours` themselves where they cover them."""
    cycles = as_schedule(rates).cycles
    averages = {}
    for winter in winters_needed(hours, rates):
        average, _ = winter_average(hours, winter_cycles(winter, cycles))
        if average is not None:
            averages[winter] = average
    return averages


def cycle_charges(
    kwh: float | None,
    gallons: float | None,
    rates: Rates,
    billed_month: int,
    electric_meters: int = 1,
    water_meters: int = 1,
    tier1_limit: float | None = None,
) -> Charges:
    """The bill for one whole billing cycle from its total usage.

    Pass None for a utility the account doesn't have. `billed_month` is the
    month the cycle ends in, which decides whether the summer rules apply:
    tiered water, and wastewater capped at the tier-1 limit but never under
    75% of use. `tier1_limit` is the winter average, used where the rates
    leave the limit at 0; with neither there are no tiers. Each meter carries its own fixed charges and, for water, its own tier-1
    allowance; with more than one water meter the gallons are taken as split
    evenly between them.
    """
    charges = Charges(garbage=rates.garbage_charge)
    if kwh is not None:
        charges += Charges(
            electric_energy=kwh * rates.electric_energy_rate,
            electric_fuel=kwh * rates.electric_fuel_adjustment_rate,
            electric_fixed=rates.electric_customer_charge * electric_meters,
        )
    if gallons is not None and water_meters > 1:
        each = cycle_charges(
            None, gallons / water_meters, rates, billed_month, tier1_limit=tier1_limit
        )
        charges += Charges(
            water_volume=each.water_volume * water_meters,
            water_fixed=each.water_fixed * water_meters,
            wastewater_volume=each.wastewater_volume * water_meters,
            wastewater_fixed=each.wastewater_fixed * water_meters,
        )
    elif gallons is not None:
        if billed_month in rates.summer_months:
            limit = rates.water_tier1_gallons or tier1_limit or inf
            tier1 = min(gallons, limit)
            sewer = _summer_sewer_gallons(gallons, limit)
        else:
            tier1 = sewer = gallons
        tier2 = gallons - tier1
        charges += Charges(
            water_volume=tier1 * rates.water_tier1_rate + tier2 * rates.water_tier2_rate,
            water_fixed=rates.water_customer_charge + rates.water_state_fee,
            wastewater_volume=sewer * rates.wastewater_rate,
            wastewater_fixed=rates.wastewater_customer_charge,
        )
    return charges


def _add_hours(when: datetime, hours: float) -> datetime:
    """`when` plus real elapsed hours, as local time.

    Adding a timedelta to a local time moves the wall clock, which is an hour
    out across a daylight-saving change, so the sum is done in UTC.
    """
    return (when.astimezone(timezone.utc) + timedelta(hours=hours)).astimezone(SITE_TZ)


@dataclass(frozen=True)
class HourRecord:
    """One hour of usage and what it cost."""

    # In UTC, so that differences between records are real elapsed time.
    start: datetime
    # Local wall-clock start, for grouping into days and months.
    local: datetime
    kwh: float | None
    gallons: float | None
    charges: Charges
    # Every billed meter reported this hour.
    complete: bool


@dataclass(frozen=True)
class PeriodSummary:
    """Usage and charges over a span of time."""

    label: str
    start: datetime
    end: datetime
    kwh: float
    gallons: float
    charges: Charges
    electric_hours: int
    water_hours: int
    complete_hours: int
    # Real hours from start to end.
    hours_total: float
    # The span has ended and its data is essentially all in.
    complete: bool

    @property
    def days(self) -> float:
        return self.hours_total / 24.0

    @property
    def coverage(self) -> float:
        """Share of the span's hours every billed meter has reported."""
        return self.complete_hours / self.hours_total if self.hours_total > 0 else 0.0

    @property
    def total(self) -> float:
        return self.charges.total

    def as_dict(self) -> dict[str, Any]:
        c = self.charges
        return {
            "period": self.label,
            "start": self.start.date().isoformat(),
            "end": self.end.date().isoformat(),
            "total": round(c.total, 2),
            "electricity": round(c.electricity, 2),
            "water": round(c.water, 2),
            "wastewater": round(c.wastewater, 2),
            "garbage": round(c.garbage, 2),
            "fixed": round(c.fixed, 2),
            "variable": round(c.variable, 2),
            "kwh": round(self.kwh, 1),
            "gallons": round(self.gallons),
            "coverage": round(self.coverage, 3),
            "complete": self.complete,
        }


class Ledger:
    """Hour records in time order, with fast sums over any span."""

    def __init__(
        self,
        records: Iterable[HourRecord],
        fuels: Iterable[str],
        winters: Mapping[int, float] | None = None,
    ) -> None:
        # Winter average per water meter, by the year of the winter's March
        # bill: the tier-1 limit of that year's summer bills.
        self.winters = dict(winters or {})
        self.records = sorted(records, key=lambda r: r.start)
        self._stamps = [r.start.timestamp() for r in self.records]
        fuels = list(fuels)
        self.electric_meters = fuels.count(FUEL_ELECTRICITY)
        self.water_meters = fuels.count(FUEL_WATER)
        self.has_electricity = self.electric_meters > 0
        self.has_water = self.water_meters > 0
        # End of the newest hour every billed meter has reported.
        self.data_through: datetime | None = next(
            (_add_hours(r.start, 1) for r in reversed(self.records) if r.complete),
            None,
        )

    def __len__(self) -> int:
        return len(self.records)

    @property
    def first(self) -> datetime | None:
        return self.records[0].start if self.records else None

    def tier1_limit(self, cycle_end: datetime) -> float | None:
        """The winter average that applies to the bill for a cycle, if known."""
        return self.winters.get(_limit_winter(*_billed_month(cycle_end)))

    def between(self, start: datetime, end: datetime) -> list[HourRecord]:
        lo = bisect_left(self._stamps, start.timestamp())
        hi = bisect_left(self._stamps, end.timestamp())
        return self.records[lo:hi]

    def span(self, start: datetime, end: datetime, label: str) -> PeriodSummary:
        """Sum the hours that start in [start, end)."""
        records = self.between(start, end)
        hours_total = elapsed_hours(start, end)
        complete_hours = sum(1 for r in records if r.complete)
        through = self.data_through
        return PeriodSummary(
            label=label,
            start=start,
            end=end,
            kwh=sum(r.kwh for r in records if r.kwh is not None),
            gallons=sum(r.gallons for r in records if r.gallons is not None),
            charges=Charges.sum(r.charges for r in records),
            electric_hours=sum(1 for r in records if r.kwh is not None),
            water_hours=sum(1 for r in records if r.gallons is not None),
            complete_hours=complete_hours,
            hours_total=hours_total,
            complete=(
                through is not None
                and end <= through
                and hours_total > 0
                and complete_hours / hours_total >= COMPLETE_COVERAGE
            ),
        )


def build_ledger(
    series: Iterable[tuple],
    rates: Rates | RateSchedule,
) -> Ledger:
    """Cost out each meter's hours and line them up by hour.

    `series` holds (fuel type, hourly usage) for each billed meter, optionally
    followed by its known winter averages by year (winters not given are
    worked out from a water meter's own hours) and then by the meter's own
    rates, when its billing cycles differ from the account's. Garbage is
    charged to the hours every meter has reported, as the total cost statistic
    does. Over those hours the ledger's total equals that statistic; an hour
    only some meters have reported keeps their charges here, as their own cost
    statistics do, so while one meter lags a period's total can run ahead of
    the total cost statistic.
    """
    usage: dict[datetime, list[float | None]] = {}
    charged: dict[datetime, list[Charges]] = {}
    fuels: list[str] = []
    winters: list[dict[int, float]] = []
    # How many meters actually reported each hour, as against being filled in.
    reported: dict[datetime, int] = {}
    for fuel, hours, *extra in series:
        known = extra[0] if extra else {}
        # Each meter is billed on its own cycles when its bills' dates differ.
        meter_rates = extra[1] if len(extra) > 1 else rates
        hours = sorted(hours, key=lambda h: h.start)
        # Charges run through the hours a meter skipped, as the stored costs
        # do; usage is only recorded for the hours it reported.
        billed = fill_gaps(hours)
        if fuel == FUEL_ELECTRICITY:
            slot, itemised = 0, electricity_charges(billed, meter_rates)
        elif fuel == FUEL_WATER:
            meter_winters = {**winter_averages(hours, meter_rates), **known}
            winters.append(meter_winters)
            slot, itemised = 1, water_charges(billed, meter_rates, meter_winters)
        else:
            continue
        fuels.append(fuel)
        measured = {h.start: h.value for h in hours}
        for h, charges in zip(billed, itemised):
            charged.setdefault(h.start, []).append(charges)
            if h.start in measured:
                values = usage.setdefault(h.start, [None, None])
                values[slot] = (values[slot] or 0.0) + h.value
                reported[h.start] = reported.get(h.start, 0) + 1

    schedule = as_schedule(rates)
    cycles = _Cycles(schedule.cycles)
    records = []
    for start in sorted(charged):
        parts = charged[start]
        complete = reported.get(start, 0) == len(fuels)
        charges = Charges.sum(parts)
        # Garbage goes on every hour all meters are costed for, measured or
        # filled in, as in the total cost statistic.
        if len(parts) == len(fuels):
            charges += Charges(
                garbage=schedule.at(start).garbage_charge / cycles.hours(start)
            )
        kwh, gallons = usage.get(start, (None, None))
        records.append(
            HourRecord(
                start.astimezone(timezone.utc),
                start.astimezone(SITE_TZ),
                kwh,
                gallons,
                charges,
                complete,
            )
        )
    # With several water meters, forecasts use the mean of their averages.
    years = {year for meter in winters for year in meter}
    limits = {
        year: sum(v := [m[year] for m in winters if year in m]) / len(v) for year in years
    }
    return Ledger(records, fuels, limits)


# --- Periods -----------------------------------------------------------------


def _local(year: int, month: int, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=SITE_TZ)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_bounds(when: datetime) -> tuple[datetime, datetime]:
    """The calendar month containing `when`, in the utility's time zone."""
    local = when.astimezone(SITE_TZ)
    return _local(local.year, local.month), _local(*_next_month(local.year, local.month))


def year_bounds(when: datetime) -> tuple[datetime, datetime]:
    local = when.astimezone(SITE_TZ)
    return _local(local.year, 1), _local(local.year + 1, 1)


def monthly(ledger: Ledger, now: datetime) -> list[PeriodSummary]:
    """One summary per calendar month from the first data to the current month."""
    if ledger.first is None:
        return []
    start, end = month_bounds(ledger.first)
    last = month_bounds(now)[0]
    months = []
    while start <= last:
        months.append(ledger.span(start, end, start.strftime("%Y-%m")))
        start, end = end, _local(*_next_month(end.year, end.month))
    return months


def yearly(ledger: Ledger, now: datetime) -> list[PeriodSummary]:
    """One summary per calendar year from the first data to the current year."""
    if ledger.first is None:
        return []
    first = ledger.first.astimezone(SITE_TZ).year
    last = now.astimezone(SITE_TZ).year
    return [
        ledger.span(_local(y, 1), _local(y + 1, 1), str(y)) for y in range(first, last + 1)
    ]


def billing_cycles(
    ledger: Ledger, billing_day: int | BillingCycles, now: datetime
) -> list[PeriodSummary]:
    """One summary per billing cycle, labelled by the day its meter read closes it."""
    if ledger.first is None:
        return []
    start, end = cycle_bounds(ledger.first, billing_day)
    last = cycle_bounds(now, billing_day)[0]
    cycles = []
    while start <= last:
        cycles.append(ledger.span(start, end, end.date().isoformat()))
        start, end = cycle_bounds(end, billing_day)
    return cycles


# --- Comparisons -------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """A figure in two periods."""

    current: float
    previous: float

    @property
    def delta(self) -> float:
        return self.current - self.previous

    @property
    def pct(self) -> float | None:
        return pct_change(self.current, self.previous)

    def as_dict(self, digits: int = 2) -> dict[str, Any]:
        pct = self.pct
        return {
            "current": round(self.current, digits),
            "previous": round(self.previous, digits),
            "change": round(self.delta, digits),
            "percent": round(pct, 1) if pct is not None else None,
        }


@dataclass(frozen=True)
class Comparison:
    """One period against an earlier one."""

    current: PeriodSummary
    previous: PeriodSummary

    def _change(self, pick) -> Change:
        return Change(pick(self.current), pick(self.previous))

    @property
    def total(self) -> Change:
        return self._change(lambda p: p.charges.total)

    @property
    def per_day(self) -> Change:
        """Total cost per day, so months of different lengths compare fairly."""
        return self._change(lambda p: p.charges.total / p.days if p.days else 0.0)

    @property
    def changes(self) -> dict[str, Change]:
        return {
            "total": self.total,
            "per_day": self.per_day,
            ELECTRICITY: self._change(lambda p: p.charges.electricity),
            WATER: self._change(lambda p: p.charges.water),
            WASTEWATER: self._change(lambda p: p.charges.wastewater),
            GARBAGE: self._change(lambda p: p.charges.garbage),
            "kwh": self._change(lambda p: p.kwh),
            "gallons": self._change(lambda p: p.gallons),
        }

    @property
    def drivers(self) -> dict[str, float]:
        """Splits the change in total cost into why it changed.

        Usage effects price the change in quantity at the earlier period's
        average rate; rate effects price the change in average rate at the
        current quantity. Together with the fixed-charge change they add up to
        the change in total.
        """
        cur, prev = self.current.charges, self.previous.charges
        e_usage, e_rate = _usage_and_rate_effects(
            self.current.kwh,
            self.previous.kwh,
            cur.electric_energy + cur.electric_fuel,
            prev.electric_energy + prev.electric_fuel,
        )
        w_usage, w_rate = _usage_and_rate_effects(
            self.current.gallons,
            self.previous.gallons,
            cur.water_volume + cur.wastewater_volume,
            prev.water_volume + prev.wastewater_volume,
        )
        return {
            "electricity_usage": e_usage,
            "electricity_rate": e_rate,
            "water_usage": w_usage,
            "water_rate": w_rate,
            "fixed_charges": cur.fixed - prev.fixed,
        }

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "current_period": self.current.label,
            "previous_period": self.previous.label,
        }
        for name, change in self.changes.items():
            out[name] = change.as_dict(1 if name in ("kwh", "gallons") else 2)
        # Adding 0.0 turns a rounded -0.0 into 0.0.
        out["drivers"] = {k: round(v, 2) + 0.0 for k, v in self.drivers.items()}
        return out


def _usage_and_rate_effects(
    quantity: float, previous_quantity: float, cost: float, previous_cost: float
) -> tuple[float, float]:
    """Split a change in usage-based cost into a usage part and a rate part."""
    if quantity <= 0 or previous_quantity <= 0:
        # No average rate on one side, so the whole change is from usage.
        return cost - previous_cost, 0.0
    rate, previous_rate = cost / quantity, previous_cost / previous_quantity
    return (quantity - previous_quantity) * previous_rate, (rate - previous_rate) * quantity


def _year_before[T: (date, datetime)](when: T) -> T:
    try:
        return when.replace(year=when.year - 1)
    except ValueError:  # 29 February
        return when.replace(year=when.year - 1, day=28)


def _same_time_last_year(when: datetime) -> datetime:
    return _year_before(when.astimezone(SITE_TZ))


def to_date_comparisons(
    ledger: Ledger, now: datetime
) -> tuple[Comparison | None, Comparison | None]:
    """This month and this year so far, each against the same stretch before.

    The month so far is compared with the same number of hours at the start of
    last month; the year so far with last year up to the same date. Each is
    None when the earlier stretch has too little data to be a fair comparison.
    """
    through = ledger.data_through
    if through is None:
        return None, None

    def fair(current: PeriodSummary, previous: PeriodSummary) -> Comparison | None:
        # A hole in either stretch would show up as a change in cost.
        if min(current.coverage, previous.coverage) < COMPLETE_COVERAGE:
            return None
        return Comparison(current, previous)

    month_start, _ = month_bounds(now)
    month = None
    if through > month_start:
        prev_start = _local(*_previous_month(month_start.year, month_start.month))
        # Both stretches are the same length. Late in a month that is longer
        # than the one before, that is the whole of the shorter month.
        hours = min(
            elapsed_hours(month_start, through), elapsed_hours(prev_start, month_start)
        )
        whole = hours == elapsed_hours(month_start, through)
        month = fair(
            ledger.span(
                month_start,
                _add_hours(month_start, hours),
                f"{month_start:%Y-%m} " + ("to date" if whole else f"first {hours / 24:.0f} days"),
            ),
            ledger.span(
                prev_start, _add_hours(prev_start, hours), f"{prev_start:%Y-%m} same days"
            ),
        )

    year_start, _ = year_bounds(now)
    year = None
    if through > year_start:
        prev_start = _local(year_start.year - 1, 1)
        year = fair(
            ledger.span(year_start, through, f"{year_start.year} to date"),
            ledger.span(
                prev_start, _same_time_last_year(through), f"{prev_start.year} same days"
            ),
        )
    return month, year


# --- Effective rates ---------------------------------------------------------


@dataclass(frozen=True)
class EffectiveRates:
    """What a unit of usage really cost over a period, fixed charges included."""

    period: PeriodSummary

    @staticmethod
    def _per(cost: float, quantity: float) -> float | None:
        return cost / quantity if quantity > 0 else None

    @property
    def electricity(self) -> float | None:
        """All-in cost per kWh: energy, fuel adjustment and customer charge."""
        return self._per(self.period.charges.electricity, self.period.kwh)

    @property
    def electricity_usage_only(self) -> float | None:
        """Cost per kWh without the customer charge: what one more kWh costs."""
        c = self.period.charges
        return self._per(c.electric_energy + c.electric_fuel, self.period.kwh)

    @property
    def electricity_fixed_share(self) -> float | None:
        c = self.period.charges
        return c.electric_fixed / c.electricity if c.electricity > 0 else None

    @property
    def water(self) -> float | None:
        """All-in cost per gallon of water service alone."""
        return self._per(self.period.charges.water, self.period.gallons)

    @property
    def water_and_wastewater(self) -> float | None:
        """All-in cost per gallon through the meter, wastewater included."""
        c = self.period.charges
        return self._per(c.water + c.wastewater, self.period.gallons)

    @property
    def water_usage_only(self) -> float | None:
        """Volume charges per gallon (water and wastewater), no fixed charges."""
        c = self.period.charges
        return self._per(c.water_volume + c.wastewater_volume, self.period.gallons)

    @property
    def total_per_day(self) -> float | None:
        return self._per(self.period.charges.total, self.period.days)


# --- Daily usage -------------------------------------------------------------


@dataclass(frozen=True)
class DayUsage:
    """One local day of one utility's usage."""

    day: date
    usage: float
    hours: int
    # Usage by local hour of day, for the hours that reported.
    profile: dict[int, float]
    # Usage-based charges for the day.
    variable_cost: float

    @property
    def full(self) -> bool:
        return self.hours >= FULL_DAY_HOURS

    @property
    def per_day(self) -> float:
        """Usage as a 24-hour rate, so 23- and 25-hour days compare evenly."""
        return self.usage * 24.0 / self.hours


def daily_usage(ledger: Ledger, utility: str) -> list[DayUsage]:
    """Daily totals for electricity or water, oldest first."""
    days: dict[date, list] = {}
    for r in ledger.records:
        if utility == ELECTRICITY:
            value = r.kwh
            cost = r.charges.electric_energy + r.charges.electric_fuel
        else:
            value = r.gallons
            cost = r.charges.water_volume + r.charges.wastewater_volume
        if value is None:
            continue
        entry = days.setdefault(r.local.date(), [0.0, 0, {}, 0.0])
        entry[0] += value
        entry[1] += 1
        entry[2][r.local.hour] = entry[2].get(r.local.hour, 0.0) + value
        entry[3] += cost
    return [DayUsage(day, *entry) for day, entry in sorted(days.items())]


# --- Forecasts ---------------------------------------------------------------


@dataclass(frozen=True)
class UsageLevel:
    """Recent typical daily usage and how much days vary around it."""

    per_day: float
    sigma: float
    days: int


def recent_level(
    days: Sequence[DayUsage],
    as_of: date,
    window: int = LEVEL_WINDOW_DAYS,
    half_life: float = LEVEL_HALF_LIFE_DAYS,
) -> UsageLevel | None:
    """Exponentially weighted mean of the full days in the `window` before `as_of`.

    Yesterday has weight 1 and a day `half_life` days older has half of that,
    so the level follows a trend without being thrown by one odd day.
    """
    points = [
        (0.5 ** (((as_of - d.day).days - 1) / half_life), d.per_day)
        for d in days
        if d.full and 0 < (as_of - d.day).days <= window
    ]
    if not points:
        return None
    weight = sum(w for w, _ in points)
    level = sum(w * v for w, v in points) / weight
    variance = sum(w * (v - level) ** 2 for w, v in points) / weight
    return UsageLevel(level, sqrt(variance), len(points))


def seasonal_drift(
    days: Sequence[DayUsage], as_of: date, until: date
) -> float | None:
    """How usage moved over the same stretch of last year.

    Last year's average daily usage from `as_of` to `until`, over its recent
    level going into that stretch. Above 1 means the season was climbing at
    this point of the year and below 1 that it was easing, so multiplying this
    year's level by it carries the season into the forecast. None when last
    year doesn't have the data.
    """
    then, then_until = _year_before(as_of), _year_before(until)
    span = (then_until - then).days
    level = recent_level(days, then)
    values = [d.per_day for d in days if d.full and then <= d.day < then_until]
    if (
        span <= 0
        or level is None
        or level.days < BASELINE_MIN_DAYS
        or level.per_day <= 0
        or len(values) < DRIFT_MIN_COVERAGE * span
    ):
        return None
    low, high = DRIFT_LIMITS
    return min(high, max(low, (sum(values) / len(values)) / level.per_day))


@dataclass(frozen=True)
class Forecast:
    """An expected bill for one billing cycle, with a likely range."""

    cycle_start: datetime
    cycle_end: datetime
    # "trend": recent daily usage carried forward, adjusted for how the season
    # moved over the same days last year when that is known. "seasonal": the
    # same cycle last year, scaled by how this year has run against last.
    method: str
    kwh: float | None
    gallons: float | None
    charges: Charges
    low: float
    high: float
    # Days of the cycle that had to be filled in rather than measured.
    days_projected: float
    # The multipliers applied to usage, e.g. {"electricity_season": 0.91}.
    factors: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return self.charges.total

    def as_dict(self) -> dict[str, Any]:
        c = self.charges
        return {
            "period_start": self.cycle_start.date().isoformat(),
            "period_end": self.cycle_end.date().isoformat(),
            "method": self.method,
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "electricity": round(c.electricity, 2),
            "water": round(c.water, 2),
            "wastewater": round(c.wastewater, 2),
            "garbage": round(c.garbage, 2),
            "kwh": round(self.kwh, 1) if self.kwh is not None else None,
            "gallons": round(self.gallons) if self.gallons is not None else None,
            "days_projected": round(self.days_projected, 1),
            "factors": {k: round(v, 3) for k, v in self.factors.items()},
        }


def _forecast_from_usage(
    start: datetime,
    end: datetime,
    method: str,
    rates: Rates,
    expected: tuple[float | None, float | None],
    spread: tuple[float, float],
    floor: tuple[float, float],
    days_projected: float,
    factors: dict[str, float],
    meters: tuple[int, int] = (1, 1),
    tier1_limit: float | None = None,
) -> Forecast:
    """Price expected usage and the usage one spread either side of it."""
    month = _billed_month(end)[1]

    def bill(direction: int) -> Charges:
        kwh, gallons = (
            None if value is None else max(low, value + direction * width)
            for value, width, low in zip(expected, spread, floor)
        )
        return cycle_charges(kwh, gallons, rates, month, *meters, tier1_limit)

    return Forecast(
        cycle_start=start,
        cycle_end=end,
        method=method,
        kwh=expected[0],
        gallons=expected[1],
        charges=bill(0),
        low=bill(-1).total,
        high=bill(1).total,
        days_projected=days_projected,
        factors=factors,
    )


def forecast_current_cycle(
    ledger: Ledger, rates: Rates | RateSchedule, now: datetime
) -> Forecast | None:
    """This cycle's bill: usage so far plus recent daily usage for the rest.

    Hours without data, both those still to come and any gaps behind, are
    filled at the recent level, adjusted for how usage moved over the same
    days last year. The whole cycle's usage is then priced as one bill, so
    water tiers and fixed charges come out as they will be billed.
    """
    through = ledger.data_through
    if through is None:
        return None
    schedule = as_schedule(rates)
    start, end = cycle_bounds(now, schedule.cycles)
    so_far = ledger.span(start, end, "")
    as_of = through.date()
    cycle_hours = elapsed_hours(start, end)

    expected: list[float | None] = []
    spread: list[float] = []
    floor: list[float] = []
    factors: dict[str, float] = {}
    missing_days = 0.0
    for utility, has, actual, hours in (
        (ELECTRICITY, ledger.has_electricity, so_far.kwh, so_far.electric_hours),
        (WATER, ledger.has_water, so_far.gallons, so_far.water_hours),
    ):
        if not has:
            expected.append(None)
            spread.append(0.0)
            floor.append(0.0)
            continue
        daily = daily_usage(ledger, utility)
        level = recent_level(daily, as_of)
        if level is None:
            return None
        days = max(0.0, cycle_hours - hours) / 24.0
        missing_days = max(missing_days, days)
        drift = seasonal_drift(daily, as_of, end.date()) if as_of < end.date() else None
        if drift is not None:
            factors[f"{utility}_season"] = drift
        expected.append(actual + level.per_day * (drift or 1.0) * days)
        spread.append(RANGE_Z * level.sigma * sqrt(days))
        floor.append(actual)
    return _forecast_from_usage(
        start,
        end,
        "trend",
        schedule.at(start),
        (expected[0], expected[1]),
        (spread[0], spread[1]),
        (floor[0], floor[1]),
        missing_days,
        factors,
        (ledger.electric_meters, ledger.water_meters),
        ledger.tier1_limit(end),
    )


def _year_earlier(
    start: datetime, end: datetime, billing_day: int | BillingCycles
) -> tuple[datetime, datetime]:
    """The billing cycle that covered the same time of year a year before."""
    middle = _add_hours(start, elapsed_hours(start, end) / 2)
    return cycle_bounds(_year_before(middle), billing_day)


def yearly_growth(
    ledger: Ledger,
    billing_day: int | BillingCycles,
    before: datetime,
    cycles: int = GROWTH_CYCLES,
) -> tuple[float | None, float | None]:
    """Usage in the last complete cycles over the same cycles a year earlier.

    Returns (electricity, water) ratios, each limited to GROWTH_LIMITS, or None
    where no cycle has a complete counterpart a year before.
    """
    recent = [0.0, 0.0]
    earlier = [0.0, 0.0]
    end = cycle_bounds(before, billing_day)[0]
    for _ in range(cycles):
        start = cycle_bounds(end - timedelta(seconds=1), billing_day)[0]
        this_year = ledger.span(start, end, "")
        last_year = ledger.span(*_year_earlier(start, end, billing_day), "")
        if this_year.complete and last_year.complete:
            recent[0] += this_year.kwh
            recent[1] += this_year.gallons
            earlier[0] += last_year.kwh
            earlier[1] += last_year.gallons
        end = start
    low, high = GROWTH_LIMITS

    def ratio(now: float, then: float) -> float | None:
        return min(high, max(low, now / then)) if then > 0 else None

    return ratio(recent[0], earlier[0]), ratio(recent[1], earlier[1])


def forecast_next_cycle(
    ledger: Ledger, rates: Rates | RateSchedule, now: datetime
) -> Forecast | None:
    """The bill after this one.

    With a complete cycle from a year earlier, that cycle's usage is scaled by
    how recent cycles compare with theirs a year before, which carries the
    season. Without one, recent daily usage is carried forward instead.
    """
    through = ledger.data_through
    if through is None:
        return None
    schedule = as_schedule(rates)
    billing_day = schedule.cycles
    current_start, current_end = cycle_bounds(now, billing_day)
    start, end = cycle_bounds(current_end, billing_day)
    days = elapsed_hours(start, end) / 24.0
    last_year = ledger.span(*_year_earlier(start, end, billing_day), "")
    growth = yearly_growth(ledger, billing_day, current_start)

    seasonal = last_year.complete
    # With no complete pair of years to compare, assume no change.
    factors = [g if g is not None else 1.0 for g in growth]
    expected: list[float | None] = []
    spread: list[float] = []
    applied: dict[str, float] = {}
    for utility, has, base, factor in (
        (ELECTRICITY, ledger.has_electricity, last_year.kwh, factors[0]),
        (WATER, ledger.has_water, last_year.gallons, factors[1]),
    ):
        if not has:
            expected.append(None)
            spread.append(0.0)
            continue
        level = recent_level(daily_usage(ledger, utility), through.date())
        if seasonal:
            # Scale to this cycle's length; last year's may differ by a day.
            expected.append(base * factor * days / last_year.days)
            applied[f"{utility}_growth"] = factor
        elif level is not None:
            expected.append(level.per_day * days)
        else:
            return None
        spread.append(RANGE_Z * (level.sigma if level else 0.0) * sqrt(days))
    return _forecast_from_usage(
        start,
        end,
        "seasonal" if seasonal else "trend",
        schedule.at(start),
        (expected[0], expected[1]),
        (spread[0], spread[1]),
        (0.0, 0.0),
        days,
        applied,
        (ledger.electric_meters, ledger.water_meters),
        ledger.tier1_limit(end),
    )


# --- Anomalies ---------------------------------------------------------------


@dataclass(frozen=True)
class Anomaly:
    """A day whose usage stands well clear of the weeks before it."""

    day: date
    utility: str
    # "spike": far above the baseline. "continuous_flow": water never stopped.
    kind: str
    usage: float
    baseline: float
    zscore: float | None
    excess_cost: float
    cause: str
    explanation: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def excess(self) -> float:
        return self.usage - self.baseline

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "utility": self.utility,
            "kind": self.kind,
            "usage": round(self.usage, 1),
            "typical": round(self.baseline, 1),
            "excess": round(self.excess, 1),
            "zscore": round(self.zscore, 1) if self.zscore is not None else None,
            "excess_cost": round(self.excess_cost, 2),
            "cause": self.cause,
            "explanation": self.explanation,
            "evidence": self.evidence,
        }


def robust_zscore(value: float, baseline: Sequence[float]) -> tuple[float, float, float]:
    """How far `value` is above the baseline's median, in robust sigmas.

    Returns (z, median, sigma). Sigma is 1.4826 x the median absolute deviation,
    which matches a standard deviation for normal data but isn't inflated by
    earlier spikes. It is floored at a share of the median so an unusually
    steady baseline doesn't make ordinary days look extreme.
    """
    mid = median(baseline)
    mad = median(abs(v - mid) for v in baseline)
    sigma = max(MAD_TO_SIGMA * mad, ANOMALY_SIGMA_FLOOR * abs(mid), 1e-9)
    return (value - mid) / sigma, mid, sigma


def _typical_profile(days: Sequence[DayUsage]) -> dict[int, float]:
    """Median usage for each hour of the day across the baseline days."""
    return {
        hour: median(d.profile.get(hour, 0.0) for d in days) for hour in range(24)
    }


def _excess_by_hour(day: DayUsage, typical: dict[int, float]) -> dict[int, float]:
    return {
        hour: max(0.0, value - typical.get(hour, 0.0))
        for hour, value in day.profile.items()
    }


def _top_share(excess: dict[int, float], count: int) -> tuple[float, list[int]]:
    """Share of the extra usage that fell in its `count` biggest hours."""
    total = sum(excess.values())
    if total <= 0:
        return 0.0, []
    top = sorted(excess, key=excess.get, reverse=True)[:count]  # type: ignore[arg-type]
    return sum(excess[h] for h in top) / total, sorted(top)


def _share_in(excess: dict[int, float], hours: range) -> float:
    total = sum(excess.values())
    return sum(v for h, v in excess.items() if h in hours) / total if total > 0 else 0.0


def _explain_electricity(
    day: DayUsage, typical: dict[int, float], had_gap: bool
) -> tuple[str, str, dict[str, Any]]:
    """Guess why a day's electricity was high from when the extra was used."""
    excess = _excess_by_hour(day, typical)
    top_share, top_hours = _top_share(excess, 3)
    overnight = range(1, 5)
    night_now = sum(day.profile.get(h, 0.0) for h in overnight) / len(overnight)
    night_typical = sum(typical.get(h, 0.0) for h in overnight) / len(overnight)
    floor_now = min(day.profile.values())
    floor_typical = min(typical.values()) if typical else 0.0
    evidence = {
        "peak_hours": top_hours,
        "share_in_peak_hours": round(top_share, 2),
        "overnight_kwh_per_hour": round(night_now, 2),
        "typical_overnight_kwh_per_hour": round(night_typical, 2),
        "lowest_hour_kwh": round(floor_now, 2),
        "typical_lowest_hour_kwh": round(floor_typical, 2),
    }
    if had_gap and top_share >= 0.6:
        return (
            "meter_catch_up",
            "Most of the extra arrived in a few reads right after missing data, "
            "so this is probably the meter catching up rather than real usage.",
            evidence,
        )
    # Every hour, even the quietest, ran well above normal.
    if floor_now >= max(2.0 * floor_typical, floor_typical + 0.5) and top_share < 0.5:
        return (
            "always_on_load",
            "Usage was raised around the clock, including the quietest hours. "
            "Something ran continuously: emergency/strip heat, a space heater, "
            "a dehumidifier, a pool pump, or equipment stuck on.",
            evidence,
        )
    if top_share >= 0.6:
        return (
            "short_heavy_load",
            f"Most of the extra fell in a few hours ({_hours_text(top_hours)}): "
            "a large appliance run, such as EV charging, the oven, dryer loads "
            "or a water heater recovery.",
            evidence,
        )
    month = day.day.month
    afternoon = _share_in(excess, range(12, 21))
    cold_hours = _share_in(excess, range(0, 10)) + _share_in(excess, range(20, 24))
    evidence["share_noon_to_9pm"] = round(afternoon, 2)
    if month in (5, 6, 7, 8, 9, 10) and afternoon >= 0.5:
        return (
            "cooling",
            "The extra was concentrated from midday into the evening in a warm "
            "month, the pattern of air conditioning working through a hot day.",
            evidence,
        )
    if month in (11, 12, 1, 2, 3) and cold_hours >= 0.5:
        return (
            "heating",
            "The extra was concentrated overnight and in the morning in a cool "
            "month, the pattern of heating through a cold snap.",
            evidence,
        )
    return (
        "broad_increase",
        "Usage was higher through most of the day without one clear peak: more "
        "people at home, guests, or weather-driven heating or cooling.",
        evidence,
    )


def _explain_water(
    day: DayUsage, typical: dict[int, float], had_gap: bool
) -> tuple[str, str, dict[str, Any]]:
    """Guess why a day's water use was high from when the extra was used."""
    excess = _excess_by_hour(day, typical)
    top_share, top_hours = _top_share(excess, 3)
    floor_now = min(day.profile.values())
    evidence = {
        "peak_hours": top_hours,
        "share_in_peak_hours": round(top_share, 2),
        "lowest_hour_gallons": round(floor_now, 1),
    }
    if had_gap and top_share >= 0.6:
        return (
            "meter_catch_up",
            "Most of the extra arrived in a few reads right after missing data, "
            "so this is probably the meter catching up rather than real usage.",
            evidence,
        )
    if day.full and floor_now >= CONTINUOUS_FLOW_MIN_GALLONS:
        return (
            "continuous_flow",
            f"Water ran in every hour of the day, never below {floor_now:.0f} "
            "gal/h. A home normally has hours with no flow, so check for a "
            "leak: a running toilet, a dripping hose bib, or a stuck "
            "irrigation valve.",
            evidence,
        )
    if top_share >= 0.6:
        early = all(h < 8 or h >= 21 for h in top_hours)
        what = (
            "an irrigation timer is the usual cause at those hours"
            if early
            else "irrigation, filling a pool or spa, or pressure washing"
        )
        return (
            "short_heavy_draw",
            f"Most of the extra was drawn in a few hours ({_hours_text(top_hours)}): {what}.",
            evidence,
        )
    return (
        "broad_increase",
        "Water use was higher through the day without one clear peak: guests, "
        "extra laundry, or several smaller outdoor uses.",
        evidence,
    )


def _hours_text(hours: Sequence[int]) -> str:
    return ", ".join(f"{h:02d}:00" for h in hours)


def detect_anomalies(
    ledger: Ledger, since: date | None = None
) -> list[Anomaly]:
    """Days from `since` on whose usage is far above their trailing baseline.

    Each full day is compared with the full days in the BASELINE_DAYS before
    it. A day is flagged when its robust z-score reaches ANOMALY_Z and it is
    also at least ANOMALY_MIN_RATIO times, and a minimum amount above, the
    baseline median. Water is also flagged when it runs for
    CONTINUOUS_FLOW_HOURS straight, which points to a leak however small.
    """
    anomalies: list[Anomaly] = []
    for utility, has in ((ELECTRICITY, ledger.has_electricity), (WATER, ledger.has_water)):
        if not has:
            continue
        days = daily_usage(ledger, utility)
        by_day = {d.day: d for d in days}
        # Days already reported as a leak by the spike check.
        leaks: set[date] = set()
        for index, day in enumerate(days):
            if not day.full or (since is not None and day.day < since):
                continue
            baseline = [
                d
                for d in days[max(0, index - BASELINE_DAYS) : index]
                if d.full and (day.day - d.day).days <= BASELINE_DAYS
            ]
            if len(baseline) < BASELINE_MIN_DAYS:
                continue
            z, typical, _ = robust_zscore(day.usage, [d.usage for d in baseline])
            excess = day.usage - typical
            if (
                z < ANOMALY_Z
                or day.usage < ANOMALY_MIN_RATIO * typical
                or excess < ANOMALY_MIN_EXCESS[utility]
            ):
                continue
            yesterday = by_day.get(day.day - timedelta(days=1))
            had_gap = yesterday is None or not yesterday.full
            explain = _explain_electricity if utility == ELECTRICITY else _explain_water
            cause, explanation, evidence = explain(
                day, _typical_profile(baseline), had_gap
            )
            anomalies.append(
                Anomaly(
                    day=day.day,
                    utility=utility,
                    kind="spike",
                    usage=day.usage,
                    baseline=typical,
                    zscore=z,
                    excess_cost=day.variable_cost * excess / day.usage,
                    cause=cause,
                    explanation=explanation,
                    evidence=evidence,
                )
            )
            if cause == "continuous_flow":
                leaks.add(day.day)
        if utility == WATER:
            anomalies.extend(_continuous_flow(ledger, days, leaks, since))
    return sorted(anomalies, key=lambda a: (a.day, a.utility))


def _continuous_flow(
    ledger: Ledger, days: Sequence[DayUsage], reported: set[date], since: date | None
) -> list[Anomaly]:
    """Runs of consecutive hours in which water never stopped flowing."""
    by_day = {d.day: d for d in days}
    found: dict[date, Anomaly] = {}
    run: list[HourRecord] = []

    def close() -> None:
        if len(run) < CONTINUOUS_FLOW_HOURS:
            return
        lowest = min(r.gallons for r in run)  # type: ignore[type-var]
        # Report it on the day the run ended, once.
        day = run[-1].local.date()
        if day in reported or day in found or (since is not None and day < since):
            return
        # Figures are for the run's last 24 hours, not its last calendar day,
        # which is usually only part-way through when the data stops.
        last_day = run[-CONTINUOUS_FLOW_HOURS:]
        usage = sum(r.gallons for r in last_day)  # type: ignore[misc]
        variable = sum(
            r.charges.water_volume + r.charges.wastewater_volume for r in last_day
        )
        # The steady trickle is what the leak itself wastes each day.
        wasted = lowest * 24.0
        cost = variable * wasted / usage if usage > 0 else 0.0
        found[day] = Anomaly(
            day=day,
            utility=WATER,
            kind="continuous_flow",
            usage=usage,
            baseline=max(0.0, usage - wasted),
            zscore=None,
            excess_cost=cost,
            cause="continuous_flow",
            explanation=(
                f"Water has run for {len(run)} hours in a row, never below "
                f"{lowest:.0f} gal/h (about {wasted:.0f} gal a day). A home "
                "normally has hours with no flow, so check for a leak: a "
                "running toilet, a dripping hose bib, or a stuck irrigation valve."
            ),
            evidence={
                "hours_running": len(run),
                "lowest_hour_gallons": round(lowest, 1),
                "running_since": run[0].local.isoformat(),
            },
        )

    previous: HourRecord | None = None
    for record in ledger.records:
        if record.gallons is None:
            continue
        adjacent = (
            previous is not None
            and record.start - previous.start == timedelta(hours=1)
        )
        running = record.gallons >= CONTINUOUS_FLOW_MIN_GALLONS
        if running and (adjacent or not run):
            run.append(record)
        else:
            close()
            run = [record] if running else []
        previous = record
    close()
    return list(found.values())


# --- Everything together -----------------------------------------------------


@dataclass(frozen=True)
class Insights:
    """Everything the insight sensors report, from one pass over the ledger."""

    data_through: datetime | None
    months: list[PeriodSummary]
    years: list[PeriodSummary]
    cycles: list[PeriodSummary]
    # Last complete calendar month against the one before it.
    month_over_month: Comparison | None
    # Last complete calendar month against the same month a year earlier.
    year_over_year: Comparison | None
    month_to_date: Comparison | None
    year_to_date: Comparison | None
    # For the last complete billing cycle, or the current one until then.
    rates: EffectiveRates | None
    forecast: Forecast | None
    forecast_next: Forecast | None
    anomalies: list[Anomaly]

    @property
    def this_month(self) -> PeriodSummary | None:
        return self.months[-1] if self.months else None

    @property
    def this_year(self) -> PeriodSummary | None:
        return self.years[-1] if self.years else None


def _last_complete(periods: Sequence[PeriodSummary]) -> int | None:
    for index in range(len(periods) - 1, -1, -1):
        if periods[index].complete:
            return index
    return None


def build_insights(
    ledger: Ledger, rates: Rates | RateSchedule, now: datetime
) -> Insights:
    """Run every analysis over the ledger as of `now`."""
    schedule = as_schedule(rates)
    months = monthly(ledger, now)
    years = yearly(ledger, now)
    cycles = billing_cycles(ledger, schedule.cycles, now)
    through = ledger.data_through

    month_over_month = year_over_year = None
    last = _last_complete(months)
    if last is not None:
        if last >= 1 and months[last - 1].complete:
            month_over_month = Comparison(months[last], months[last - 1])
        if last >= 12 and months[last - 12].complete:
            year_over_year = Comparison(months[last], months[last - 12])
    month_to_date, year_to_date = to_date_comparisons(ledger, now)

    effective = None
    last_cycle = _last_complete(cycles)
    if last_cycle is not None:
        effective = EffectiveRates(cycles[last_cycle])
    elif cycles and cycles[-1].complete_hours:
        effective = EffectiveRates(cycles[-1])

    since = through.date() - timedelta(days=ANOMALY_REPORT_DAYS) if through else None
    return Insights(
        data_through=through,
        months=months,
        years=years,
        cycles=cycles,
        month_over_month=month_over_month,
        year_over_year=year_over_year,
        month_to_date=month_to_date,
        year_to_date=year_to_date,
        rates=effective,
        forecast=forecast_current_cycle(ledger, schedule, now),
        forecast_next=forecast_next_cycle(ledger, schedule, now),
        anomalies=detect_anomalies(ledger, since),
    )
