"""Tests for the estimated bill figures and sensors."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.lus import coordinator as coord
from custom_components.lus.api import HourlyUsage, Meter
from custom_components.lus.cost import SITE_TZ, BillEstimate, Rates, bill_estimates
from custom_components.lus.sensor import (
    SENSORS,
    LusBillSensor,
    LusWinterAverageSensor,
    projected_change,
)

CDT = timezone(timedelta(hours=-5))


def local(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=SITE_TZ)


def hourly(start: datetime, end: datetime, value: float) -> list[HourlyUsage]:
    count = int((end - start) / timedelta(hours=1))
    return [HourlyUsage(start + timedelta(hours=i), value) for i in range(count)]


# Whole 9/23-10/23 cycle at $0.50/h, then 10/23 through 10/29 23:00 at $1/h
HOURS = hourly(local(2026, 9, 23), local(2026, 10, 23), 0.5) + hourly(
    local(2026, 10, 23), local(2026, 10, 30), 1.0
)
NOW = local(2026, 10, 31, 9)


NO_WINTER = coord.WinterAverageData(2026, None, None)


def change_data(projected: float | None, previous: float = 100.0, *, complete: bool = True):
    """Build bill data for projected-change unit tests without portal or HA setup."""
    current_start, current_end = local(2026, 10, 23), local(2026, 11, 23)
    previous_start = local(2026, 9, 23)
    current = BillEstimate(
        current_start,
        current_end,
        # One day's share of the cycle, which has 745 hours: 31 days plus the
        # hour daylight saving gives back.
        0.0 if projected is None else projected * 24 / 745,
        None if projected is None else current_start + timedelta(days=1),
    )
    previous_bill = BillEstimate(
        previous_start,
        current_start,
        previous,
        current_start if complete else current_start - timedelta(hours=1),
    )
    return coord.LusData("5000000000", current, previous_bill, NO_WINTER)


def test_projected_change_higher():
    assert projected_change(change_data(125.25)) == pytest.approx(25.25)


def test_projected_change_lower():
    assert projected_change(change_data(75.5)) == pytest.approx(-24.5)


def test_projected_change_equal():
    assert projected_change(change_data(100.0)) == pytest.approx(0.0)


def test_projected_change_unavailable_without_projection_or_previous_data():
    assert projected_change(change_data(None)) is None
    data = change_data(125.0)
    previous = BillEstimate(data.previous.cycle_start, data.previous.cycle_end, 0.0, None)
    assert projected_change(coord.LusData(data.account_id, data.current, previous, NO_WINTER)) is None


def test_projected_change_unavailable_with_incomplete_previous_cycle():
    assert projected_change(change_data(125.0, complete=False)) is None


def test_current_and_previous_cycles():
    current, previous = bill_estimates(HOURS, 23, NOW)

    assert (current.cycle_start, current.cycle_end) == (local(2026, 10, 23), local(2026, 11, 23))
    assert current.cost == pytest.approx(7 * 24 * 1.0)
    assert current.data_through == local(2026, 10, 30)
    assert not current.complete

    assert (previous.cycle_start, previous.cycle_end) == (local(2026, 9, 23), local(2026, 10, 23))
    assert previous.cost == pytest.approx(30 * 24 * 0.5)
    assert previous.complete


def test_projection_scales_to_the_whole_cycle():
    current, _ = bill_estimates(HOURS, 23, NOW)
    # $1 per elapsed hour over the fall-DST 10/23-11/23 cycle.
    assert current.projected == pytest.approx(31 * 24 + 1)


@pytest.mark.parametrize(
    ("start", "end", "through", "elapsed_hours"),
    [
        (local(2026, 2, 23), local(2026, 3, 25), local(2026, 3, 1), 144),
        (local(2026, 10, 23), local(2026, 11, 22), local(2026, 10, 30), 168),
    ],
)
def test_projection_preserves_fixed_charge_across_dst(
    start, end, through, elapsed_hours
):
    whole_hours = (end.timestamp() - start.timestamp()) / 3600
    fixed_charge = whole_hours
    accumulated = elapsed_hours * fixed_charge / whole_hours

    estimate = BillEstimate(start, end, accumulated, through)

    assert estimate.projected == pytest.approx(fixed_charge)


def test_cycle_without_data():
    current, previous = bill_estimates([], 23, NOW)
    assert current.cost == 0 and current.data_through is None
    assert current.projected is None
    assert not previous.complete


def make_sensor(key, data):
    description = next(d for d in SENSORS if d.key == key)
    coordinator = SimpleNamespace(data=data)
    return LusBillSensor(coordinator, "5000000000", "USD", description)


def test_sensors_report_rounded_dollars_and_period():
    current, previous = bill_estimates(HOURS, 23, NOW)
    data = coord.LusData(
        "5000000000",
        current,
        previous,
        coord.WinterAverageData(2026, "1000000002", 20470.456),
    )

    this_period = make_sensor("current_bill", data)
    assert this_period.native_value == 168.0
    assert this_period.native_unit_of_measurement == "USD"
    assert this_period.unique_id == "5000000000_current_bill"
    assert this_period.extra_state_attributes == {
        "period_start": "2026-10-23",
        "period_end": "2026-11-23",
        "data_through": "2026-10-29T23:59:59-05:00",
        "complete": False,
    }
    # The 10/23-11/23 cycle has 745 hours: 31 days plus the hour daylight saving gives back.
    assert make_sensor("projected_bill", data).native_value == 745.0
    change = make_sensor("projected_bill_change", data)
    assert change.native_value == 385.0
    assert change.unique_id == "5000000000_projected_bill_change"
    assert make_sensor("projected_bill_change", change_data(125.556)).native_value == 25.56
    last = make_sensor("last_bill", data)
    assert last.native_value == 360.0
    assert last.extra_state_attributes["complete"] is True


def test_winter_average_sensor_reports_gallons_and_source():
    current, previous = bill_estimates(HOURS, 23, NOW)
    data = coord.LusData(
        "5000000000",
        current,
        previous,
        coord.WinterAverageData(2026, "1000000002", 20470.456, True),
    )
    sensor = LusWinterAverageSensor(SimpleNamespace(data=data), "5000000000")

    assert sensor.native_value == 20470.46
    assert sensor.native_unit_of_measurement == "gal"
    assert sensor.unique_id == "5000000000_winter_water_average"
    assert sensor.extra_state_attributes == {
        "winter_year": 2026,
        "water_meter_id": "1000000002",
        "complete": True,
    }


def test_winter_average_sensor_reports_partial_winter():
    current, previous = bill_estimates(HOURS, 23, NOW)
    data = coord.LusData(
        "5000000000",
        current,
        previous,
        coord.WinterAverageData(2026, "1000000002", 21000.0, False),
    )
    sensor = LusWinterAverageSensor(SimpleNamespace(data=data), "5000000000")

    assert sensor.native_value == 21000.0
    assert sensor.extra_state_attributes["complete"] is False


def test_winter_average_sensor_is_unknown_without_usable_complete_reads():
    current, previous = bill_estimates(HOURS, 23, NOW)
    data = coord.LusData(
        "5000000000", current, previous, coord.WinterAverageData(2026, None, None)
    )
    sensor = LusWinterAverageSensor(SimpleNamespace(data=data), "5000000000")

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {
        "winter_year": 2026,
        "water_meter_id": None,
        "complete": False,
    }


def test_sensors_are_unknown_before_first_data():
    sensor = make_sensor("current_bill", None)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_total_cost_reads_back_to_the_previous_cycle():
    meter = Meter("u", "1", "ELECTRICITY", "QUARTER_HOUR", date(2023, 9, 30))
    last = (local(2026, 10, 29, 23), 100.0)
    resume = coord.LusCoordinator._resume_date(
        last, meter, date(2026, 10, 31), 23, previous_cycle=True
    )
    assert resume == date(2026, 9, 23)
    assert coord.LusCoordinator._resume_date(last, meter, date(2026, 10, 31), 23) == date(2026, 10, 23)
