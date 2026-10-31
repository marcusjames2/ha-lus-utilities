"""Tests for the bill cost model."""

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.lus.api import HourlyUsage
from custom_components.lus.cost import (
    SITE_TZ,
    BillingCycles,
    Rates,
    RateSchedule,
    bill_estimates,
    cycle_bounds,
    electricity_costs,
    fill_gaps,
    garbage_cost,
    water_costs,
    winter_average,
    winter_cycles,
    winters_needed,
)
from custom_components.lus.coordinator import total_costs

RATES = Rates(water_tier1_gallons=20470)  # the October bill's tier-1 limit

# The October 2026 bill's cycle: read 09/23, read 10/23.
CYCLE_START = datetime(2026, 9, 23, tzinfo=SITE_TZ)
CYCLE_END = datetime(2026, 10, 23, tzinfo=SITE_TZ)


def spread(total: float, start: datetime = CYCLE_START, end: datetime = CYCLE_END) -> list[HourlyUsage]:
    """`total` spread evenly over every hour from start to end."""
    count = int((end - start) / timedelta(hours=1))
    return [HourlyUsage(start + timedelta(hours=i), total / count) for i in range(count)]


def test_cycle_bounds():
    assert cycle_bounds(datetime(2026, 10, 1, tzinfo=SITE_TZ), 23) == (CYCLE_START, CYCLE_END)
    assert cycle_bounds(CYCLE_END, 23)[0] == CYCLE_END
    # Crosses a year boundary.
    start, end = cycle_bounds(datetime(2027, 1, 5, tzinfo=SITE_TZ), 23)
    assert (start.month, start.year, end.month, end.year) == (12, 2026, 1, 2027)


def test_electric_matches_october_bill():
    costs = electricity_costs(spread(2147), RATES)
    assert sum(c.value for c in costs) == pytest.approx(219.63, abs=0.01)


def test_water_and_wastewater_match_october_bill():
    # Water $53.52 + wastewater $164.20, including the summer discount on the
    # 830 gallons above the tier-1 limit.
    costs = water_costs(spread(21300), RATES)
    assert sum(c.value for c in costs) == pytest.approx(53.52 + 164.20, abs=0.01)


def test_summer_rules_run_april_through_november():
    # LUS schedules W1 and S1.
    assert RATES.summer_months == {4, 5, 6, 7, 8, 9, 10, 11}


def test_winter_months_have_no_tiers_and_no_discount():
    winter = Rates(wastewater_summer_months="6,7,8")
    costs = water_costs(spread(21300), winter)
    # Every gallon at the tier-1 rate and every gallon of wastewater.
    expected = 21300 * (0.00209 + 0.00747) + 6.99 + 1.00 + 11.29
    assert sum(c.value for c in costs) == pytest.approx(expected, abs=0.01)


def test_summer_wastewater_is_at_least_75_percent_of_use():
    costs = water_costs(spread(40000), RATES)
    expected = (
        20470 * 0.00209 + (40000 - 20470) * 0.00331
        + 0.75 * 40000 * 0.00747  # more than the 20,470 gal tier-1 limit
        + 6.99 + 1.00 + 11.29
    )
    assert sum(c.value for c in costs) == pytest.approx(expected, abs=0.01)


def test_water_tiers_reset_each_cycle():
    next_end = datetime(2026, 11, 23, tzinfo=SITE_TZ)
    hours = spread(21300) + spread(21300, CYCLE_END, next_end)
    costs = water_costs(hours, RATES)
    first = sum(c.value for c in costs if c.start < CYCLE_END)
    second = sum(c.value for c in costs if c.start >= CYCLE_END)
    # Same usage, same tier split, same cost in both cycles.
    assert first == pytest.approx(second, abs=0.01)


def test_whole_bill_current_charges():
    elec = electricity_costs(spread(2147), RATES)
    water = water_costs(spread(21300), RATES)
    total = total_costs([elec, water], RATES)
    assert sum(t.value for t in total) == pytest.approx(475.39, abs=0.02)


def test_total_only_covers_hours_every_meter_reported():
    elec = electricity_costs(spread(100), RATES)
    water = water_costs(spread(1000)[:-24], RATES)  # water a day behind
    total = total_costs([elec, water], RATES)
    assert len(total) == len(water)
    assert total[-1].start == water[-1].start


def test_fill_gaps_adds_zero_use_hours_between_reported_ones():
    hours = spread(720)[:10]
    del hours[3:6]
    filled = fill_gaps(hours)
    assert [h.start for h in filled] == [h.start for h in spread(720)[:10]]
    assert [h.value for h in filled[3:6]] == [0.0, 0.0, 0.0]
    assert fill_gaps([]) == []


def test_fill_gaps_counts_real_hours_across_dst():
    # 11/1/2026: 1 a.m. happens twice; the reads skip from midnight to 3 a.m.
    midnight = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)
    real = [HourlyUsage((midnight + timedelta(hours=i)).astimezone(SITE_TZ), 1.0) for i in range(5)]
    assert [h.start for h in fill_gaps([real[0], real[-1]])] == [h.start for h in real]


def test_a_skipped_water_hour_keeps_the_rest_of_the_total():
    elec = electricity_costs(spread(2147), RATES)
    water_hours = spread(21300)
    del water_hours[100:135]  # 35 hours the portal skipped
    water = water_costs(fill_gaps(water_hours), RATES)
    total = total_costs([elec, water], RATES)

    assert len(total) == len(elec)
    assert sum(t.value for t in total) == pytest.approx(
        sum(e.value for e in elec) + sum(w.value for w in water) + 38.04
    )
    # Water keeps all of its fixed charges and loses only the skipped use.
    skipped = 35 * 21300 / 720
    same_use = water_costs(spread(21300 - skipped), RATES)
    assert sum(w.value for w in water) == pytest.approx(sum(w.value for w in same_use))


def test_garbage_spreads_over_cycle():
    hours = spread(0)
    assert sum(garbage_cost(h.start, RATES) for h in hours) == pytest.approx(38.04)


def winter(*gallons: float) -> list[HourlyUsage]:
    """The December-March bills of winter 2025-26, each using its gallons evenly."""
    return [h for (s, e), g in zip(WINTER, gallons) for h in spread(g, s, e)]


WINTER = winter_cycles(2026, 23)
APRIL_2027 = (datetime(2027, 3, 23, tzinfo=SITE_TZ), datetime(2027, 4, 23, tzinfo=SITE_TZ))


def test_winter_cycles():
    assert [(s.date(), e.date()) for s, e in WINTER] == [
        (date(2025, 11, 23), date(2025, 12, 23)),
        (date(2025, 12, 23), date(2026, 1, 23)),
        (date(2026, 1, 23), date(2026, 2, 23)),
        (date(2026, 2, 23), date(2026, 3, 23)),
    ]
    # With reads on the 1st, a cycle ending on the 1st is the prior month's bill.
    first_of_month = winter_cycles(2026, 1)
    assert first_of_month[0][0].date() == date(2025, 12, 1)
    assert first_of_month[-1][1].date() == date(2026, 4, 1)


def test_winter_average_is_the_mean_of_december_through_march_bills():
    average, complete = winter_average(winter(20000, 22000, 18000, 24000), WINTER)
    assert (average, complete) == (pytest.approx(21000), True)


def test_winter_average_scales_up_a_bill_missing_a_few_hours():
    hours = winter(20000, 22000, 18000, 24000)
    del hours[800:812]  # 12 hours from the middle of January's
    average, complete = winter_average(hours, WINTER)
    assert (average, complete) == (pytest.approx(21000), True)


def test_winter_average_skips_a_bill_missing_more_than_a_day():
    hours = winter(20000, 22000, 18000, 24000)
    del hours[800:800 + 10 * 24]  # 10 days from the middle of January's
    average, complete = winter_average(hours, WINTER)
    assert (average, complete) == (pytest.approx((20000 + 18000 + 24000) / 3), False)


def test_winter_average_skips_a_bill_whose_last_reads_are_not_in():
    hours = winter(20000, 22000, 18000, 24000)[:-2 * 24]
    average, complete = winter_average(hours, WINTER)
    assert (average, complete) == (pytest.approx((20000 + 22000 + 18000) / 3), False)
    assert winter_average([], WINTER) == (None, False)


def test_automatic_tier_limit_is_the_previous_winters_average():
    costs = water_costs(spread(21300), Rates(), {2026: 20470})
    assert sum(c.value for c in costs) == pytest.approx(53.52 + 164.20, abs=0.01)
    april = water_costs(spread(21300, *APRIL_2027), Rates(), {2027: 20470})
    assert sum(c.value for c in april) == pytest.approx(53.52 + 164.20, abs=0.01)


def test_automatic_tier_limit_without_winter_data_has_no_tiers():
    costs = water_costs(spread(21300), Rates())
    # Every gallon at the tier-1 rate and every gallon of wastewater.
    expected = 21300 * (0.00209 + 0.00747) + 6.99 + 1.00 + 11.29
    assert sum(c.value for c in costs) == pytest.approx(expected, abs=0.01)


def test_a_limit_from_the_bill_wins_over_the_winter_average():
    costs = water_costs(spread(21300), RATES, {2026: 5000})
    assert sum(c.value for c in costs) == pytest.approx(53.52 + 164.20, abs=0.01)


def test_winters_needed_only_for_summer_bills_with_an_automatic_limit():
    october = spread(100)
    january = spread(100, datetime(2026, 12, 23, tzinfo=SITE_TZ), datetime(2027, 1, 23, tzinfo=SITE_TZ))
    assert winters_needed(october, Rates()) == {2026: CYCLE_START}
    assert winters_needed(october, RATES) == {}
    assert winters_needed(january, Rates()) == {}
    # A winter bill counted as summer has no finished winter to go by.
    assert winters_needed(january, Rates(wastewater_summer_months="1")) == {}


def test_april_and_november_bills_use_that_years_winter():
    april = spread(100, *APRIL_2027)
    november = spread(100, CYCLE_END, datetime(2026, 11, 23, tzinfo=SITE_TZ))
    assert winters_needed(april, Rates()) == {2027: APRIL_2027[0]}
    assert winters_needed(november, Rates()) == {2026: CYCLE_END}
    # Read on the 1st: 4/1-5/1 is the April bill.
    first_of_month = spread(100, datetime(2027, 4, 1, tzinfo=SITE_TZ), datetime(2027, 5, 1, tzinfo=SITE_TZ))
    assert winters_needed(first_of_month, Rates(billing_day=1)) == {2027: first_of_month[0].start}


def local(*args) -> datetime:
    return datetime(*args, tzinfo=SITE_TZ)


# Made-up bill periods: like LUS, they don't start on the same day every month.
BILLS = [
    (local(2026, 7, 22), local(2026, 8, 21)),
    (local(2026, 8, 21), local(2026, 9, 23)),
    (local(2026, 9, 23), local(2026, 10, 22)),
]


def test_bill_cycles_follow_the_bills():
    cycles = BillingCycles(BILLS)
    assert cycles.bounds(local(2026, 8, 25)) == BILLS[1]
    assert cycles.bounds(local(2026, 9, 22, 23)) == BILLS[1]
    assert cycles.bounds(local(2026, 9, 23)) == BILLS[2]
    assert cycles.last_bill_end == local(2026, 10, 22)


def test_bill_cycles_carry_on_monthly_beyond_the_bills():
    cycles = BillingCycles(BILLS)
    # The open cycle is taken to end a month after the last bill, on its day.
    assert cycles.bounds(local(2026, 10, 30)) == (local(2026, 10, 22), local(2026, 11, 22))
    assert cycles.bounds(local(2026, 12, 1)) == (local(2026, 11, 22), local(2026, 12, 22))
    # Before the first bill, monthly on its day, ending where it starts.
    assert cycles.bounds(local(2026, 7, 1)) == (local(2026, 6, 22), local(2026, 7, 22))


def test_bill_cycles_without_bills_use_the_default_day():
    assert BillingCycles().bounds(local(2026, 10, 1)) == (CYCLE_START, CYCLE_END)
    assert BillingCycles().last_bill_end is None


def test_a_day_in_the_options_wins_over_the_bills():
    bills = BillingCycles(BILLS)
    assert RateSchedule().with_bill_cycles(bills).cycles.bounds(local(2026, 8, 25)) == BILLS[1]
    by_day = RateSchedule([(None, Rates(billing_day=10))]).with_bill_cycles(bills)
    assert by_day.cycles.bounds(local(2026, 8, 25)) == (local(2026, 8, 10), local(2026, 9, 10))


def test_water_tiers_reset_where_the_bill_does():
    # 10 gal on each side of the 8/21 bill boundary, with a 10 gal limit.
    hours = [HourlyUsage(local(2026, 8, 20, 22), 10.0), HourlyUsage(local(2026, 8, 21, 1), 10.0)]
    usage_only = Rates(
        water_tier1_gallons=10, wastewater_rate=0,
        water_customer_charge=0, water_state_fee=0, wastewater_customer_charge=0,
    )
    schedule = RateSchedule([(None, usage_only)])
    by_bill = water_costs(hours, schedule.with_bill_cycles(BillingCycles(BILLS)))
    by_day = water_costs(hours, schedule)  # both hours in the 7/23-8/23 cycle
    assert [c.value for c in by_bill] == pytest.approx([10 * 0.00209, 10 * 0.00209])
    assert [c.value for c in by_day] == pytest.approx([10 * 0.00209, 10 * 0.00331])


def test_bill_estimates_use_the_bill_dates():
    hours = spread(100, local(2026, 9, 23), local(2026, 10, 22))
    current, previous = bill_estimates(hours, BillingCycles(BILLS), local(2026, 10, 30))
    assert (previous.cycle_start, previous.cycle_end) == BILLS[2]
    assert (current.cycle_start, current.cycle_end) == (local(2026, 10, 22), local(2026, 11, 22))
    assert previous.complete


def test_winter_cycles_are_the_winter_bills_periods():
    winter_bills = [
        (local(2025, 11, 21), local(2025, 12, 22)),
        (local(2025, 12, 22), local(2026, 1, 23)),
        (local(2026, 1, 23), local(2026, 2, 20)),
        (local(2026, 2, 20), local(2026, 3, 24)),
    ]
    assert winter_cycles(2026, BillingCycles(winter_bills)) == winter_bills


def test_rates_round_trip_through_options():
    rates = Rates(electric_fuel_adjustment_rate=0.05, billing_day=20)
    assert Rates.from_options({**rates.as_options(), "unknown": 1}) == rates
