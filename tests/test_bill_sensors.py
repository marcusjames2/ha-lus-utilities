"""Tests for the estimated bill figures and sensors."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.lus import coordinator as coord
from custom_components.lus.api import HourlyUsage, Meter
from custom_components.lus.cost import SITE_TZ, Rates, bill_estimates
from custom_components.lus.sensor import SENSORS, LusBillSensor

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
    # $1/h over the 31-day 10/23-11/23 cycle
    assert current.projected == pytest.approx(31 * 24 * 1.0)


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
    data = coord.LusData("5000000000", current, previous)

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
    assert make_sensor("projected_bill", data).native_value == 744.0
    last = make_sensor("last_bill", data)
    assert last.native_value == 360.0
    assert last.extra_state_attributes["complete"] is True


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
