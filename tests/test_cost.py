"""Tests for the bill cost model."""

from datetime import datetime, timedelta

import pytest

from custom_components.lus.api import HourlyUsage
from custom_components.lus.cost import (
    SITE_TZ,
    Rates,
    cycle_bounds,
    electricity_costs,
    garbage_cost,
    water_costs,
)
from custom_components.lus.coordinator import total_costs

RATES = Rates()

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


def test_wastewater_discount_only_in_summer_months():
    winter = Rates(wastewater_summer_months="6,7,8")
    costs = water_costs(spread(21300), winter)
    # No discount: 830 gal more of wastewater.
    assert sum(c.value for c in costs) == pytest.approx(53.52 + 164.20 + 6.20, abs=0.01)


def test_water_tiers_reset_each_cycle():
    next_end = datetime(2026, 11, 23, tzinfo=SITE_TZ)
    hours = spread(21300) + spread(21300, CYCLE_END, next_end)
    costs = water_costs(hours, Rates(wastewater_summer_months=""))
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


def test_garbage_spreads_over_cycle():
    hours = spread(0)
    assert sum(garbage_cost(h.start, RATES) for h in hours) == pytest.approx(38.04)


def test_rates_round_trip_through_options():
    rates = Rates(electric_fuel_adjustment_rate=0.05, billing_day=20)
    assert Rates.from_options({**rates.as_options(), "unknown": 1}) == rates
