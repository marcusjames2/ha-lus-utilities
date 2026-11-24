"""Tests for the spending analytics.

The worked examples in docs/MATH.md use the same numbers as these tests, so
each formula there is checked here.
"""

from datetime import date, datetime, timedelta, timezone
import math

import pytest

from custom_components.lus import analytics as an
from custom_components.lus.analytics import elapsed_hours
from custom_components.lus.api import FUEL_ELECTRICITY, FUEL_WATER, HourlyUsage
from custom_components.lus.coordinator import total_costs
from custom_components.lus.cost import (
    SITE_TZ,
    Rates,
    RateSchedule,
    electricity_costs,
    water_costs,
)

UTC = timezone.utc
# The tier-1 limit on that bill, which newer versions otherwise work out from the meter.
RATES = Rates(water_tier1_gallons=20470)

# The October 2026 bill: read 09/23, read 10/23, 2,147 kWh and 21,300 gallons.
OCT_START = datetime(2026, 9, 23, tzinfo=SITE_TZ)
OCT_END = datetime(2026, 10, 23, tzinfo=SITE_TZ)
BILL_KWH = 2147
BILL_GALLONS = 21300


def local(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=SITE_TZ)


def series(start: datetime, end: datetime, value) -> list[HourlyUsage]:
    """Hourly usage over every real hour from start to end.

    `value` is a number, or a function of the hour's local start time.
    """
    hours = []
    t = start.astimezone(UTC)
    while t < end.astimezone(UTC):
        hours.append(HourlyUsage(t, value(t.astimezone(SITE_TZ)) if callable(value) else value))
        t += timedelta(hours=1)
    return hours


def spread(total: float, start: datetime = OCT_START, end: datetime = OCT_END) -> list[HourlyUsage]:
    count = elapsed_hours(start, end)
    return series(start, end, total / count)


def ledger_of(electric=None, water=None, rates=RATES) -> an.Ledger:
    meters = []
    if electric is not None:
        meters.append((FUEL_ELECTRICITY, electric))
    if water is not None:
        meters.append((FUEL_WATER, water))
    return an.build_ledger(meters, rates)


def bill_ledger() -> an.Ledger:
    return ledger_of(spread(BILL_KWH), spread(BILL_GALLONS))


# --- Charges -----------------------------------------------------------------


def test_itemised_charges_add_up_to_the_cost_model_every_hour():
    # Two cycles, the second one past the tier-1 limit and out of summer.
    end = local(2026, 11, 23)
    electric = spread(BILL_KWH) + spread(1500, OCT_END, end)
    water = spread(BILL_GALLONS) + spread(26000, OCT_END, end)

    for charges, cost in zip(an.electricity_charges(electric, RATES), electricity_costs(electric, RATES)):
        assert charges.total == pytest.approx(cost.value, abs=1e-12)
    for charges, cost in zip(an.water_charges(water, RATES), water_costs(water, RATES)):
        assert charges.total == pytest.approx(cost.value, abs=1e-12)


def test_cycle_charges_reproduce_the_october_bill():
    charges = an.cycle_charges(BILL_KWH, BILL_GALLONS, RATES, billed_month=10)

    assert charges.electric_energy == pytest.approx(112.16, abs=0.005)
    assert charges.electric_fuel == pytest.approx(94.47, abs=0.005)
    assert charges.electric_fixed == 13.00
    assert charges.electricity == pytest.approx(219.63, abs=0.005)
    # 20,470 gal at tier 1 and 830 gal at tier 2, plus $6.99 and the $1 fee
    assert charges.water == pytest.approx(53.52, abs=0.005)
    # Summer discount: wastewater on the 20,470 tier-1 gallons only
    assert charges.wastewater == pytest.approx(164.20, abs=0.005)
    assert charges.garbage == 38.04
    assert charges.total == pytest.approx(475.39, abs=0.01)
    assert charges.fixed == pytest.approx(13.00 + 6.99 + 1.00 + 11.29 + 38.04)
    assert charges.fixed + charges.variable == pytest.approx(charges.total)


def test_cycle_charges_without_the_summer_discount_or_a_water_meter():
    winter = an.cycle_charges(BILL_KWH, BILL_GALLONS, RATES, billed_month=1)
    assert winter.wastewater == pytest.approx(164.20 + 830 * 0.00747, abs=0.005)
    # December-March bills have no tier 2 either.
    assert winter.water == pytest.approx(21300 * 0.00209 + 7.99)

    electric_only = an.cycle_charges(BILL_KWH, None, RATES, billed_month=10)
    assert electric_only.total == pytest.approx(219.63 + 38.04, abs=0.005)
    assert electric_only.water == 0 and electric_only.wastewater == 0


def test_ledger_total_matches_the_total_cost_statistic():
    electric = spread(BILL_KWH)
    # Water stops a day early, as it does on the portal.
    water = spread(BILL_GALLONS)[:-24]
    ledger = ledger_of(electric, water)

    statistic = total_costs([electricity_costs(electric, RATES), water_costs(water, RATES)], RATES)
    complete = [r for r in ledger.records if r.complete]
    assert len(complete) == len(statistic) == len(water)
    assert sum(r.charges.total for r in complete) == pytest.approx(sum(h.value for h in statistic))
    # Garbage is only charged to hours both meters reported.
    assert all(r.charges.garbage == 0 for r in ledger.records if not r.complete)
    assert ledger.data_through == local(2026, 10, 22)


def test_fixed_charges_are_whole_across_a_daylight_saving_change():
    # 10/23-11/23 holds the extra hour of 1 November.
    start, end = OCT_END, local(2026, 11, 23)
    assert elapsed_hours(start, end) == 31 * 24 + 1
    ledger = ledger_of(series(start, end, 1.0), series(start, end, 10.0))
    assert len(ledger) == 745
    charges = ledger.span(start, end, "").charges
    assert charges.fixed == pytest.approx(13.00 + 6.99 + 1.00 + 11.29 + 38.04)


def test_two_meters_of_one_fuel_are_added_together():
    ledger = an.build_ledger(
        [(FUEL_ELECTRICITY, spread(1000)), (FUEL_ELECTRICITY, spread(500))], RATES
    )
    cycle = ledger.span(OCT_START, OCT_END, "")
    assert cycle.kwh == pytest.approx(1500)
    # Each meter carries its own customer charge.
    assert cycle.charges.electric_fixed == pytest.approx(26.00)


# --- Periods -----------------------------------------------------------------

# Steady usage from 1 July 2025 through 29 October 2026.
STEADY_START = local(2025, 7, 1)
STEADY_END = local(2026, 10, 30)
NOW = local(2026, 10, 31, 9)


@pytest.fixture(scope="module")
def steady() -> an.Ledger:
    return ledger_of(series(STEADY_START, STEADY_END, 2.0), series(STEADY_START, STEADY_END, 25.0))


def test_monthly_breakdown_by_utility(steady):
    months = an.monthly(steady, NOW)
    assert [m.label for m in months][:2] == ["2025-07", "2025-08"]
    assert months[-1].label == "2026-10"

    september = next(m for m in months if m.label == "2026-09")
    assert september.complete and september.coverage == 1.0
    assert september.kwh == pytest.approx(2.0 * 720)
    assert september.gallons == pytest.approx(25.0 * 720)
    c = september.charges
    assert c.electric_energy + c.electric_fuel == pytest.approx(1440 * 0.09624)
    # 22 days of the 8/23-9/23 cycle (31 days) and 8 of the 9/23-10/23 one (30).
    share = 22 / 31 + 8 / 30
    assert c.electric_fixed == pytest.approx(13.00 * share)
    assert c.garbage == pytest.approx(38.04 * share)
    assert september.as_dict()["total"] == round(c.total, 2)
    assert sum(c.by_utility().values()) == pytest.approx(c.total)

    october = months[-1]
    assert not october.complete
    assert october.coverage == pytest.approx(29 / 31)


def test_annual_breakdown(steady):
    years = an.yearly(steady, NOW)
    assert [y.label for y in years] == ["2025", "2026"]
    y2025 = years[0]
    hours = elapsed_hours(STEADY_START, local(2026, 1, 1))
    assert y2025.kwh == pytest.approx(2.0 * hours)
    assert not y2025.complete  # data starts in July
    assert years[1].charges.total == pytest.approx(
        sum(m.charges.total for m in an.monthly(steady, NOW) if m.label.startswith("2026"))
    )


def test_billing_cycles_are_labelled_by_their_read_date(steady):
    cycles = an.billing_cycles(steady, 23, NOW)
    october = next(c for c in cycles if c.label == "2026-10-23")
    assert (october.start, october.end) == (OCT_START, OCT_END)
    assert october.complete
    # A whole cycle carries each fixed charge exactly once.
    assert october.charges.fixed == pytest.approx(70.32)
    assert cycles[-1].label == "2026-11-23" and not cycles[-1].complete


# --- Effective rates ---------------------------------------------------------


def test_effective_rates_for_the_october_bill():
    cycle = bill_ledger().span(OCT_START, OCT_END, "2026-10-23")
    rates = an.EffectiveRates(cycle)

    # $219.63 / 2,147 kWh
    assert rates.electricity == pytest.approx(0.10229, abs=1e-5)
    assert rates.electricity_usage_only == pytest.approx(0.09624)
    assert rates.electricity_fixed_share == pytest.approx(13.00 / 219.63, abs=1e-4)
    # $53.52 / 21,300 gal, and with wastewater ($53.52 + $164.20) / 21,300 gal
    assert rates.water == pytest.approx(0.002513, abs=5e-7)
    assert rates.water_and_wastewater == pytest.approx(0.010222, abs=5e-7)
    assert rates.water_usage_only == pytest.approx((45.53 + 152.91) / 21300, abs=5e-7)
    assert rates.total_per_day == pytest.approx(475.39 / 30, abs=0.001)


def test_effective_rates_are_unknown_without_usage():
    empty = ledger_of(series(OCT_START, OCT_END, 0.0), series(OCT_START, OCT_END, 0.0))
    rates = an.EffectiveRates(empty.span(OCT_START, OCT_END, ""))
    assert rates.electricity is None and rates.water is None


# --- Comparisons -------------------------------------------------------------


def test_pct_change():
    assert an.pct_change(427.39, 472.45) == pytest.approx(-9.54, abs=0.005)
    assert an.pct_change(110, 100) == pytest.approx(10.0)
    assert an.pct_change(5, 0) is None


def usage_step(change_at: datetime, before: float, after: float):
    return lambda t: before if t < change_at else after


def test_month_over_month_and_its_drivers():
    start, end = local(2026, 7, 1), local(2026, 10, 1)
    # August: 2 kWh/h and 25 gal/h. September: 2.5 kWh/h and 30 gal/h.
    ledger = ledger_of(
        series(start, end, usage_step(local(2026, 9, 1), 2.0, 2.5)),
        series(start, end, usage_step(local(2026, 9, 1), 25.0, 30.0)),
    )
    insights = an.build_insights(ledger, RATES, local(2026, 10, 2))
    change = insights.month_over_month
    assert (change.current.label, change.previous.label) == ("2026-09", "2026-08")

    kwh = change.changes["kwh"]
    assert (kwh.current, kwh.previous) == (pytest.approx(1800), pytest.approx(1488))
    assert kwh.pct == pytest.approx((1800 - 1488) / 1488 * 100)
    total = change.total
    assert total.pct == pytest.approx(total.delta / total.previous * 100)
    # September has a day less, so cost per day rose by more than the total.
    assert change.per_day.pct > total.pct

    drivers = change.drivers
    assert drivers["electricity_usage"] == pytest.approx((1800 - 1488) * 0.09624)
    assert drivers["electricity_rate"] == pytest.approx(0, abs=1e-9)
    # The pieces account for the whole change.
    assert sum(drivers.values()) == pytest.approx(total.delta)
    assert change.as_dict()["total"]["percent"] == round(total.pct, 1)


def test_drivers_separate_a_rate_change_from_a_usage_change():
    start, end = local(2026, 8, 1), local(2026, 10, 1)
    dearer = Rates(electric_fuel_adjustment_rate=0.054)
    schedule = RateSchedule([(None, RATES), (date(2026, 9, 1), dearer)])
    ledger = ledger_of(series(start, end, 2.0), series(start, end, 25.0), schedule)
    months = an.monthly(ledger, local(2026, 10, 2))
    change = an.Comparison(months[1], months[0])

    # Same 2 kWh/h, one day fewer, one cent more per kWh.
    assert change.drivers["electricity_usage"] == pytest.approx(-48 * 0.09624)
    assert change.drivers["electricity_rate"] == pytest.approx(1440 * 0.01)
    assert sum(change.drivers.values()) == pytest.approx(change.total.delta)


def test_year_over_year_needs_the_same_month_a_year_earlier(steady):
    insights = an.build_insights(steady, RATES, NOW)
    change = insights.year_over_year
    assert (change.current.label, change.previous.label) == ("2026-09", "2025-09")
    assert change.total.pct == pytest.approx(0, abs=1e-6)

    short = ledger_of(series(local(2026, 1, 1), STEADY_END, 2.0), series(local(2026, 1, 1), STEADY_END, 25.0))
    assert an.build_insights(short, RATES, NOW).year_over_year is None


def test_to_date_comparisons_use_the_same_stretch(steady):
    month, year = an.to_date_comparisons(steady, NOW)
    # Data runs through 29 October: 29 days against the first 29 of September.
    assert month.current.hours_total == month.previous.hours_total == 29 * 24
    assert month.changes["kwh"].pct == pytest.approx(0)
    # 2025 only has data from July, so the year so far has no fair comparison.
    assert year is None

    start = local(2025, 1, 1)
    two_years = ledger_of(
        series(start, STEADY_END, usage_step(local(2026, 1, 1), 2.0, 2.2)),
        series(start, STEADY_END, 25.0),
    )
    _, year = an.to_date_comparisons(two_years, NOW)
    assert (year.previous.start, year.previous.end) == (start, local(2025, 10, 30))
    assert year.changes["kwh"].pct == pytest.approx(10.0)


# --- Forecasts ---------------------------------------------------------------


def day_usage(day: date, usage: float) -> an.DayUsage:
    return an.DayUsage(day, usage, 24, {}, 0.0)


def test_recent_level_weights_newer_days_more():
    as_of = date(2026, 10, 30)
    days = [
        day_usage(date(2026, 10, 15), 40.0),  # 15 days back
        day_usage(date(2026, 10, 22), 50.0),  # 8 days back
        day_usage(date(2026, 10, 29), 60.0),  # yesterday
    ]
    level = an.recent_level(days, as_of)
    # Weights 0.5^(14/7), 0.5^(7/7), 0.5^0 = 0.25, 0.5, 1
    assert level.per_day == pytest.approx((0.25 * 40 + 0.5 * 50 + 1 * 60) / 1.75)
    assert level.per_day == pytest.approx(54.2857, abs=1e-4)
    variance = (0.25 * (40 - level.per_day) ** 2 + 0.5 * (50 - level.per_day) ** 2 + (60 - level.per_day) ** 2) / 1.75
    assert level.sigma == pytest.approx(math.sqrt(variance))
    assert level.sigma == pytest.approx(7.2843, abs=1e-4)
    assert level.days == 3

    # Days outside the window, short days and the day itself are left out.
    extra = [*days, day_usage(date(2026, 9, 1), 999.0), an.DayUsage(date(2026, 10, 28), 999.0, 12, {}, 0.0)]
    assert an.recent_level(extra, as_of).per_day == pytest.approx(level.per_day)
    assert an.recent_level([], as_of) is None


def test_forecast_of_steady_usage_is_the_exact_bill(steady):
    forecast = an.forecast_current_cycle(steady, RATES, NOW)
    assert (forecast.cycle_start, forecast.cycle_end) == (OCT_END, local(2026, 11, 23))
    hours = 31 * 24 + 1
    assert forecast.kwh == pytest.approx(2.0 * hours)
    assert forecast.gallons == pytest.approx(25.0 * hours)
    # Seven days are measured; the other 24 and an hour are filled in.
    assert forecast.days_projected == pytest.approx((hours - 168) / 24)
    expected = an.cycle_charges(2.0 * hours, 25.0 * hours, RATES, billed_month=11)
    assert forecast.total == pytest.approx(expected.total)
    # Nothing varies, so the range collapses onto the forecast.
    assert forecast.low == pytest.approx(forecast.total) == pytest.approx(forecast.high)
    assert forecast.method == "trend"


def test_forecast_prices_water_tiers_on_the_whole_cycle():
    # 40 gal/h runs past the 20,470 gal tier-1 limit late in the cycle.
    start, through = local(2026, 8, 1), local(2026, 10, 1)
    ledger = ledger_of(series(start, through, 2.0), series(start, through, 40.0))
    forecast = an.forecast_current_cycle(ledger, RATES, local(2026, 10, 1, 12))
    gallons = 40.0 * 720
    assert forecast.gallons == pytest.approx(gallons)
    assert forecast.charges.water_volume == pytest.approx(
        20470 * 0.00209 + (gallons - 20470) * 0.00331
    )
    # Scaling the cost so far would miss tier 2 altogether.
    so_far = ledger.span(OCT_START, OCT_END, "").charges.water_volume
    assert so_far * 30 / 8 < forecast.charges.water_volume


def test_forecast_range_widens_with_variation():
    start, through = local(2026, 9, 1), local(2026, 10, 30)
    noisy = lambda t: 2.0 + (1.0 if t.day % 2 else -1.0)  # noqa: E731
    ledger = ledger_of(series(start, through, noisy), series(start, through, 25.0))
    forecast = an.forecast_current_cycle(ledger, RATES, NOW)
    assert forecast.low < forecast.total < forecast.high
    days = forecast.days_projected
    level = an.recent_level(an.daily_usage(ledger, an.ELECTRICITY), date(2026, 10, 30))
    width = an.RANGE_Z * level.sigma * math.sqrt(days) * 0.09624
    assert forecast.high - forecast.total == pytest.approx(width)


def seasonal_kwh(scale_from: datetime | None = None, factor: float = 1.0):
    """Hourly kWh peaking in late July, with an afternoon peak each day."""

    def kwh(t: datetime) -> float:
        season = 1.6 + 1.4 * math.cos((t.timetuple().tm_yday - 205) / 365 * 2 * math.pi)
        shape = 1 + 0.5 * math.sin((t.hour - 9) / 24 * 2 * math.pi)
        return season * shape * (factor if scale_from and t >= scale_from else 1.0)

    return kwh


def test_forecast_follows_the_season_when_last_year_is_known():
    start = local(2025, 7, 1)
    whole = series(start, local(2026, 11, 23), seasonal_kwh())
    known = [h for h in whole if h.start < STEADY_END]
    ledger = ledger_of(known, series(start, STEADY_END, 25.0))

    forecast = an.forecast_current_cycle(ledger, RATES, NOW)
    actual = sum(h.value for h in whole if h.start >= OCT_END)
    # Usage is falling into November; carrying the level forward overshoots.
    assert forecast.factors["electricity_season"] < 0.8
    assert forecast.kwh == pytest.approx(actual, rel=0.03)
    naive = sum(h.value for h in known if h.start >= OCT_END) / 7 * (745 / 24)
    assert abs(naive - actual) > 5 * abs(forecast.kwh - actual)


def test_next_bill_uses_last_year_scaled_by_growth():
    start = local(2025, 7, 1)
    # From 2026 on, usage runs 10% above the same day a year before.
    electric = series(start, STEADY_END, seasonal_kwh(local(2026, 1, 1), 1.1))
    ledger = ledger_of(electric, series(start, STEADY_END, 25.0))

    forecast = an.forecast_next_cycle(ledger, RATES, NOW)
    assert (forecast.cycle_start, forecast.cycle_end) == (local(2026, 11, 23), local(2026, 12, 23))
    assert forecast.method == "seasonal"
    assert forecast.factors["electricity_growth"] == pytest.approx(1.1, abs=0.005)
    assert forecast.factors["water_growth"] == pytest.approx(1.0)
    last_year = ledger.span(local(2025, 11, 23), local(2025, 12, 23), "")
    assert forecast.kwh == pytest.approx(last_year.kwh * 1.1, rel=0.005)
    expected = an.cycle_charges(forecast.kwh, forecast.gallons, RATES, billed_month=12)
    assert forecast.total == pytest.approx(expected.total)


def test_next_bill_falls_back_to_the_trend_without_last_year():
    start = local(2026, 8, 1)
    ledger = ledger_of(series(start, STEADY_END, 2.0), series(start, STEADY_END, 25.0))
    forecast = an.forecast_next_cycle(ledger, RATES, NOW)
    assert forecast.method == "trend" and forecast.factors == {}
    assert forecast.kwh == pytest.approx(48.0 * 30)


def test_growth_is_limited():
    start = local(2025, 7, 1)
    electric = series(start, STEADY_END, usage_step(local(2026, 1, 1), 1.0, 5.0))
    ledger = ledger_of(electric, series(start, STEADY_END, 25.0))
    growth = an.yearly_growth(ledger, 23, OCT_END)
    assert growth == (1.5, pytest.approx(1.0))


def test_no_forecast_without_data():
    empty = ledger_of([], [])
    assert an.forecast_current_cycle(empty, RATES, NOW) is None
    assert an.forecast_next_cycle(empty, RATES, NOW) is None
    insights = an.build_insights(empty, RATES, NOW)
    assert insights.months == [] and insights.anomalies == []
    assert insights.this_month is None and insights.rates is None


# --- Anomalies ---------------------------------------------------------------


def test_robust_zscore_ignores_an_earlier_spike():
    baseline = [48, 50, 52, 47, 51, 49, 53, 50, 48, 52, 51, 49, 50, 120]
    z, typical, sigma = an.robust_zscore(86.0, baseline)
    assert typical == 50.0
    # Median absolute deviation 1.5 gives 2.22; the floor of 10% of 50 wins.
    assert sigma == pytest.approx(5.0)
    assert z == pytest.approx(7.2)

    wide = [30, 40, 50, 60, 70, 35, 45, 55, 65, 50, 42, 58, 48, 52]
    z, typical, sigma = an.robust_zscore(86.0, wide)
    assert typical == 50.0
    assert sigma == pytest.approx(1.4826 * 8.0)
    assert z == pytest.approx(36 / 11.8608, abs=1e-3)


SPIKE_DAY = date(2026, 10, 20)
A_START = local(2026, 9, 1)


def base_kwh(t: datetime) -> float:
    return 2.5 if 14 <= t.hour < 20 else 1.5  # 42 kWh a day


def base_gallons(t: datetime) -> float:
    return {6: 60.0, 7: 80.0, 18: 90.0, 19: 110.0}.get(t.hour, 0.0)  # 340 gal a day


def with_extra(base, day: date, extra):
    """`base` plus `extra(hour)` on one local day."""
    return lambda t: base(t) + (extra(t.hour) if t.date() == day else 0.0)


def anomalies_for(electric=base_kwh, water=base_gallons, rates=RATES, end=STEADY_END):
    ledger = ledger_of(series(A_START, end, electric), series(A_START, end, water), rates)
    return an.detect_anomalies(ledger, date(2026, 9, 30))


def test_ordinary_days_are_not_anomalies():
    assert anomalies_for() == []
    # 20% above normal is noticeable but not unusual enough to flag.
    assert anomalies_for(electric=with_extra(base_kwh, SPIKE_DAY, lambda h: 0.3)) == []


def test_electricity_spike_in_a_few_hours():
    extra = lambda h: 7.0 if h in (1, 2, 3) else 0.0  # noqa: E731
    (anomaly,) = anomalies_for(electric=with_extra(base_kwh, SPIKE_DAY, extra))
    assert (anomaly.day, anomaly.utility, anomaly.kind) == (SPIKE_DAY, "electricity", "spike")
    assert anomaly.usage == pytest.approx(63.0) and anomaly.baseline == pytest.approx(42.0)
    assert anomaly.excess == pytest.approx(21.0)
    # Sigma is the 10% floor, 4.2 kWh: z = 21 / 4.2
    assert anomaly.zscore == pytest.approx(5.0)
    assert anomaly.excess_cost == pytest.approx(21 * 0.09624)
    assert anomaly.cause == "short_heavy_load"
    assert anomaly.evidence["peak_hours"] == [1, 2, 3]
    assert "01:00, 02:00, 03:00" in anomaly.explanation
    assert anomaly.as_dict()["excess_cost"] == 2.02


def test_electricity_raised_around_the_clock():
    (anomaly,) = anomalies_for(electric=with_extra(base_kwh, SPIKE_DAY, lambda h: 2.0))
    assert anomaly.cause == "always_on_load"
    assert anomaly.evidence["lowest_hour_kwh"] == 3.5
    assert anomaly.evidence["typical_lowest_hour_kwh"] == 1.5


def test_electricity_hot_afternoon_and_cold_morning():
    afternoon = lambda h: 2.5 if 12 <= h < 21 else 0.0  # noqa: E731
    (hot,) = anomalies_for(electric=with_extra(base_kwh, SPIKE_DAY, afternoon))
    assert hot.cause == "cooling"

    # The same shape of day in January reads as heating.
    start, end, day = local(2025, 12, 1), local(2026, 1, 25), date(2026, 1, 20)
    morning = lambda h: 2.5 if h < 9 else 0.0  # noqa: E731
    ledger = ledger_of(
        series(start, end, with_extra(base_kwh, day, morning)), series(start, end, base_gallons)
    )
    (cold,) = an.detect_anomalies(ledger)
    assert cold.cause == "heating"


def test_water_spike_from_irrigation():
    extra = lambda h: 300.0 if h in (4, 5) else 0.0  # noqa: E731
    (anomaly,) = anomalies_for(water=with_extra(base_gallons, SPIKE_DAY, extra))
    assert anomaly.utility == "water" and anomaly.cause == "short_heavy_draw"
    assert "irrigation timer" in anomaly.explanation
    assert anomaly.excess == pytest.approx(600.0)
    # October is a summer-discount month and the cycle is under the tier-1
    # limit, so each extra gallon costs the tier-1 and wastewater rates.
    assert anomaly.excess_cost == pytest.approx(600 * (0.00209 + 0.00747))


def test_water_running_every_hour_is_reported_as_a_leak():
    # A toilet starts running at midnight on 10/25: 6 gal/h, every hour.
    leak_from = local(2026, 10, 25)
    leaking = lambda t: base_gallons(t) + (6.0 if t >= leak_from else 0.0)  # noqa: E731
    found = anomalies_for(water=leaking)

    leaks = [a for a in found if a.cause == "continuous_flow"]
    assert leaks and all(a.utility == "water" for a in found)
    latest = leaks[-1]
    assert latest.day == date(2026, 10, 29)
    assert latest.evidence["lowest_hour_gallons"] == 6.0
    assert "leak" in latest.explanation
    # One report per day at most.
    assert len({a.day for a in found}) == len(found)


def test_small_steady_leak_is_found_without_a_spike():
    # 1 gal/h is far too little to stand out in the daily total.
    leak_from = local(2026, 10, 27)
    leaking = lambda t: base_gallons(t) + (1.0 if t >= leak_from else 0.0)  # noqa: E731
    (anomaly,) = anomalies_for(water=leaking)
    assert (anomaly.kind, anomaly.day) == ("continuous_flow", date(2026, 10, 29))
    assert anomaly.zscore is None
    assert anomaly.evidence["hours_running"] == 72
    assert anomaly.evidence["running_since"] == "2026-10-27T00:00:00-05:00"
    assert anomaly.excess == pytest.approx(24.0)
    assert anomaly.excess_cost == pytest.approx(24 * (0.00209 + 0.00747))


def test_catch_up_read_after_a_gap_is_called_out():
    electric = series(A_START, STEADY_END, base_kwh)
    # The meter drops out for 10/19 and reports it all in one read on 10/20.
    missing = [h for h in electric if h.start.astimezone(SITE_TZ).date() == date(2026, 10, 19)]
    catch_up = sum(h.value for h in missing)
    electric = [h for h in electric if h not in missing]
    first = next(i for i, h in enumerate(electric) if h.start.astimezone(SITE_TZ).date() == SPIKE_DAY)
    electric[first] = HourlyUsage(electric[first].start, electric[first].value + catch_up)

    ledger = ledger_of(electric, series(A_START, STEADY_END, base_gallons))
    (anomaly,) = an.detect_anomalies(ledger, date(2026, 9, 30))
    assert anomaly.day == SPIKE_DAY and anomaly.cause == "meter_catch_up"


def test_anomalies_need_enough_history():
    extra = lambda h: 7.0 if h in (1, 2, 3) else 0.0  # noqa: E731
    start = local(2026, 10, 10)
    ledger = ledger_of(
        series(start, STEADY_END, with_extra(base_kwh, SPIKE_DAY, extra)),
        series(start, STEADY_END, base_gallons),
    )
    # Only ten days before the spike: no baseline to judge it against.
    assert an.detect_anomalies(ledger) == []


# --- Everything together -----------------------------------------------------


def test_insights_cover_every_question(steady):
    insights = an.build_insights(steady, RATES, NOW)
    assert insights.data_through == STEADY_END
    assert insights.this_month.label == "2026-10"
    assert insights.this_year.label == "2026"
    assert insights.month_over_month.current.label == "2026-09"
    assert insights.month_to_date is not None
    # Effective rates come from the bill that last closed.
    assert insights.rates.period.label == "2026-10-23"
    assert insights.rates.electricity == pytest.approx((1440 * 0.09624 + 13) / 1440)
    assert insights.forecast.total > insights.cycles[-1].total
    assert insights.forecast_next.cycle_start == local(2026, 11, 23)
    # This made-up home draws 25 gal every hour of the day, which a real one
    # only does with a leak.
    assert [a.cause for a in insights.anomalies] == ["continuous_flow"]


def test_insights_use_the_current_cycle_until_one_closes():
    ledger = ledger_of(series(OCT_END, STEADY_END, 2.0), series(OCT_END, STEADY_END, 25.0))
    insights = an.build_insights(ledger, RATES, NOW)
    assert insights.rates.period.label == "2026-11-23"
    assert not insights.rates.period.complete
    assert insights.month_over_month is None and insights.year_over_year is None


# --- The worked examples in docs/MATH.md -------------------------------------


def test_math_example_september_by_calendar_month(steady):
    september = next(m for m in an.monthly(steady, NOW) if m.label == "2026-09")
    assert round(september.charges.electricity, 2) == 151.28
    assert round(september.charges.electric_fixed, 2) == 12.69
    assert round(september.charges.garbage, 2) == 37.14


def test_math_example_month_over_month():
    start, end = local(2026, 7, 1), local(2026, 10, 1)
    ledger = ledger_of(
        series(start, end, usage_step(local(2026, 9, 1), 2.0, 2.5)),
        series(start, end, usage_step(local(2026, 9, 1), 25.0, 30.0)),
    )
    change = an.build_insights(ledger, RATES, local(2026, 10, 2)).month_over_month
    out = change.as_dict()
    assert out["kwh"]["percent"] == 21.0
    assert (out["total"]["previous"], out["total"]["current"]) == (391.34, 443.57)
    assert out["total"]["percent"] == 13.3
    assert (out["per_day"]["previous"], out["per_day"]["current"]) == (12.62, 14.79)
    assert out["per_day"]["percent"] == 17.1
    assert out["drivers"] == {
        "electricity_usage": 30.03,
        "electricity_rate": 0.0,
        "water_usage": 28.68,
        "water_rate": -4.81,
        "fixed_charges": -1.66,
    }


def test_math_example_forecast_this_cycle():
    days = (745 - 168) / 24
    forecast = an._forecast_from_usage(
        OCT_END,
        local(2026, 11, 23),
        "trend",
        RATES,
        expected=(430 + 58 * 0.90 * days, 4900 + 690 * days),
        spread=(an.RANGE_Z * 6 * math.sqrt(days), an.RANGE_Z * 150 * math.sqrt(days)),
        floor=(430, 4900),
        days_projected=days,
        factors={"electricity_season": 0.90},
    )
    assert round(days, 2) == 24.04
    assert round(forecast.kwh, 1) == 1685.0 and round(forecast.gallons, 1) == 21488.8
    c = forecast.charges
    assert [round(v, 2) for v in (c.electricity, c.water, c.wastewater, c.garbage)] == [
        175.16, 54.14, 164.20, 38.04,
    ]
    assert round(forecast.total, 2) == 431.55
    assert (round(forecast.low, 2), round(forecast.high, 2)) == (424.80, 438.30)


def test_math_example_next_bill():
    charges = an.cycle_charges(1250 * 1.08, 19800 * 0.97, RATES, billed_month=12)
    assert [round(v, 2) for v in (charges.electricity, charges.water, charges.wastewater)] == [
        142.92, 48.13, 154.76,
    ]
    assert round(charges.total, 2) == 383.85


def test_math_example_leak_cost():
    assert round(24 * (0.00209 + 0.00747), 2) == 0.23
    assert round(600 * (0.00209 + 0.00747), 2) == 5.74


# --- Cases found in review ---------------------------------------------------


def test_month_to_date_never_compares_unequal_lengths():
    # Flat usage, data through 30 March: February has only 28 days.
    start, through = local(2026, 12, 1), local(2027, 3, 31)
    ledger = ledger_of(series(start, through, 2.0), series(start, through, base_gallons))
    month, _ = an.to_date_comparisons(ledger, local(2027, 3, 31, 9))
    assert month.current.hours_total == month.previous.hours_total == 28 * 24
    assert month.current.label == "2027-03 first 28 days"
    assert month.changes["kwh"].pct == pytest.approx(0)


def test_to_date_comparison_is_withheld_when_this_month_has_a_hole():
    start = local(2026, 8, 1)
    hole = (local(2026, 10, 10), local(2026, 10, 20))
    keep = lambda hours: [h for h in hours if not hole[0] <= h.start < hole[1]]  # noqa: E731
    ledger = ledger_of(keep(series(start, STEADY_END, 2.0)), keep(series(start, STEADY_END, 25.0)))
    month, _ = an.to_date_comparisons(ledger, NOW)
    assert month is None


def test_a_month_is_not_complete_before_it_ends():
    # Through 30 October is 97% of the month, but the month isn't over.
    start, through = local(2026, 7, 1), local(2026, 10, 31)
    ledger = ledger_of(series(start, through, 2.0), series(start, through, 25.0))
    insights = an.build_insights(ledger, RATES, local(2026, 10, 31, 9))
    assert insights.months[-1].coverage > an.COMPLETE_COVERAGE
    assert not insights.months[-1].complete
    assert insights.month_over_month.current.label == "2026-09"


def test_comparisons_skip_months_with_missing_data():
    start = local(2025, 7, 1)

    def without(first: datetime, last: datetime) -> an.Ledger:
        keep = lambda hours: [h for h in hours if not first <= h.start < last]  # noqa: E731
        return ledger_of(keep(series(start, STEADY_END, 2.0)), keep(series(start, STEADY_END, 25.0)))

    # Ten days of September missing: the last complete month is August.
    insights = an.build_insights(without(local(2026, 9, 5), local(2026, 9, 15)), RATES, NOW)
    change = insights.month_over_month
    assert (change.current.label, change.previous.label) == ("2026-08", "2026-07")
    assert insights.year_over_year.current.label == "2026-08"
    # August missing: September has nothing whole to be compared with.
    insights = an.build_insights(without(local(2026, 8, 5), local(2026, 8, 15)), RATES, NOW)
    assert insights.month_over_month is None
    # The same month a year ago was only half there.
    insights = an.build_insights(without(local(2025, 9, 1), local(2025, 9, 16)), RATES, NOW)
    assert insights.year_over_year is None


def test_growth_has_a_lower_limit_and_skips_incomplete_years():
    start = local(2025, 7, 1)
    electric = series(start, STEADY_END, usage_step(local(2026, 1, 1), 5.0, 1.0))
    ledger = ledger_of(electric, series(start, STEADY_END, 25.0))
    assert an.yearly_growth(ledger, 23, OCT_END)[0] == 0.5

    # Two weeks missing a year ago would otherwise look like 10% growth.
    hole = (local(2025, 9, 25), local(2025, 10, 9))
    electric = [h for h in series(start, STEADY_END, 2.0) if not hole[0] <= h.start < hole[1]]
    ledger = ledger_of(electric, series(start, STEADY_END, 25.0))
    assert an.yearly_growth(ledger, 23, OCT_END)[0] == pytest.approx(1.0)


def test_next_bill_has_a_range_and_differs_from_this_one():
    start = local(2025, 7, 1)
    noisy = lambda t: seasonal_kwh()(t) * (1.3 if t.day % 2 else 0.7)  # noqa: E731
    ledger = ledger_of(series(start, STEADY_END, noisy), series(start, STEADY_END, 25.0))
    this, following = an.forecast_current_cycle(ledger, RATES, NOW), an.forecast_next_cycle(ledger, RATES, NOW)
    assert following.low < following.total < following.high
    assert this.low < this.total < this.high
    assert following.total != pytest.approx(this.total, abs=1.0)


def test_forecast_charges_each_meter_its_own_fixed_charges():
    start = local(2026, 8, 1)
    ledger = an.build_ledger(
        [
            (FUEL_ELECTRICITY, series(start, STEADY_END, 1.0)),
            (FUEL_ELECTRICITY, series(start, STEADY_END, 1.0)),
            (FUEL_WATER, series(start, STEADY_END, 25.0)),
        ],
        RATES,
    )
    forecast = an.forecast_current_cycle(ledger, RATES, NOW)
    assert forecast.charges.electric_fixed == pytest.approx(26.00)
    two_water = an.cycle_charges(None, 30000, RATES, 10, water_meters=2)
    # 15,000 gal on each meter stays in tier 1; one meter would not.
    assert two_water.water_volume == pytest.approx(30000 * 0.00209)
    assert two_water.water_fixed == pytest.approx(2 * 7.99)


def test_leak_cost_does_not_depend_on_the_hour_the_data_stops():
    leak_from = local(2026, 10, 27)
    leaking = lambda t: base_gallons(t) + (1.0 if t >= leak_from else 0.0)  # noqa: E731
    for end in (local(2026, 10, 30, 1), local(2026, 10, 30, 5), local(2026, 10, 30, 12)):
        (anomaly,) = anomalies_for(water=leaking, end=end)
        assert anomaly.day == date(2026, 10, 30)
        assert anomaly.as_dict()["excess_cost"] == 0.23
        assert anomaly.usage == pytest.approx(364.0) and anomaly.excess == pytest.approx(24.0)


def test_season_decides_between_cooling_and_heating():
    afternoon = lambda h: 2.5 if 12 <= h < 21 else 0.0  # noqa: E731
    start, end, day = local(2025, 12, 1), local(2026, 1, 25), date(2026, 1, 20)
    ledger = ledger_of(
        series(start, end, with_extra(base_kwh, day, afternoon)), series(start, end, base_gallons)
    )
    (winter,) = an.detect_anomalies(ledger)
    assert winter.cause != "cooling"


def test_year_before_handles_leap_day():
    assert an._year_before(date(2028, 2, 29)) == date(2027, 2, 28)


# --- Water schedules W1 and S1 -----------------------------------------------


def test_summer_wastewater_is_never_charged_on_under_three_quarters_of_use():
    # 40,000 gal against a 20,470 limit: 75% of use is more than the limit.
    charges = an.cycle_charges(None, 40000, RATES, billed_month=8)
    assert charges.wastewater_volume == pytest.approx(0.75 * 40000 * 0.00747)
    assert charges.water_volume == pytest.approx(20470 * 0.00209 + 19530 * 0.00331)


def test_tier_limit_comes_from_the_winter_average_when_the_rates_leave_it_out():
    automatic = Rates()
    assert automatic.water_tier1_gallons == 0
    # Without a winter average there are no tiers and no discount.
    plain = an.cycle_charges(None, 30000, automatic, billed_month=8)
    assert plain.water_volume == pytest.approx(30000 * 0.00209)
    assert plain.wastewater_volume == pytest.approx(30000 * 0.00747)
    known = an.cycle_charges(None, 30000, automatic, billed_month=8, tier1_limit=20000)
    assert known.water_volume == pytest.approx(20000 * 0.00209 + 10000 * 0.00331)
    assert known.wastewater_volume == pytest.approx(0.75 * 30000 * 0.00747)


def test_ledger_works_out_the_winter_average_and_matches_the_cost_model():
    # 25 gal/h through last winter, 45 gal/h since: summer bills pass the limit.
    start = local(2025, 11, 23)
    water = series(start, STEADY_END, usage_step(local(2026, 4, 23), 25.0, 45.0))
    automatic = Rates()
    ledger = ledger_of(series(start, STEADY_END, 2.0), water, automatic)

    limit = 25 * (720 + 744 + 744 + 671) / 4
    assert ledger.winters == {2026: pytest.approx(limit)}
    assert ledger.tier1_limit(OCT_END) == pytest.approx(limit)
    assert ledger.tier1_limit(local(2026, 1, 23)) is None  # a winter bill has no tiers

    expected = water_costs(water, automatic, {2026: limit})
    for record, cost in zip(ledger.records, expected):
        c = record.charges
        assert c.water + c.wastewater == pytest.approx(cost.value, abs=1e-12)
    october = ledger.span(OCT_START, OCT_END, "").charges
    gallons = 45.0 * 720
    assert october.water_volume == pytest.approx(limit * 0.00209 + (gallons - limit) * 0.00331)
    assert october.wastewater_volume == pytest.approx(0.75 * gallons * 0.00747)

    # A winter average read from the portal takes precedence.
    given = an.build_ledger([(FUEL_WATER, water, {2026: 20000.0})], automatic)
    assert given.winters == {2026: 20000.0}

    forecast = an.forecast_current_cycle(ledger, automatic, NOW)
    assert forecast.charges.wastewater_volume == pytest.approx(0.75 * forecast.gallons * 0.00747)
