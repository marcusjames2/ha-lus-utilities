"""Tests for the spending insight sensors and how the coordinator feeds them."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.lus import analytics as an, coordinator as coord
from custom_components.lus.api import HourlyUsage, Meter
from custom_components.lus.cost import SITE_TZ, Rates, as_schedule, bill_estimates
from custom_components.lus.sensor import INSIGHT_SENSORS, SENSORS, LusInsightSensor

UTC = timezone.utc
# The October 2026 bill, with its tier-1 limit.
RATES = Rates(water_tier1_gallons=20470)
ACCOUNT = "5000000000"
ELECTRIC = Meter("elec-uuid", "1000000001", "ELECTRICITY", "QUARTER_HOUR", date(2025, 7, 1))
WATER = Meter("water-uuid", "1000000002", "WATER", "HOUR", date(2025, 7, 1))
START = datetime(2025, 7, 1, tzinfo=SITE_TZ)
END = datetime(2026, 10, 30, tzinfo=SITE_TZ)
NOW = datetime(2026, 10, 31, 9, tzinfo=SITE_TZ)


def series(start: datetime, end: datetime, value) -> list[HourlyUsage]:
    hours = []
    t = start.astimezone(UTC)
    while t < end.astimezone(UTC):
        hours.append(HourlyUsage(t, value(t.astimezone(SITE_TZ))))
        t += timedelta(hours=1)
    return hours


def gallons(t: datetime) -> float:
    return {6: 150.0, 7: 150.0, 18: 150.0, 19: 150.0}.get(t.hour, 0.0)


@pytest.fixture(scope="module")
def insights() -> an.Insights:
    ledger = an.build_ledger(
        [
            ("ELECTRICITY", series(START, END, lambda t: 2.0)),
            ("WATER", series(START, END, gallons)),
        ],
        RATES,
    )
    return an.build_insights(ledger, RATES, NOW)


def make_sensor(key: str, insights: an.Insights | None, with_data: bool = True):
    description = next(d for d in INSIGHT_SENSORS if d.key == key)
    data = None
    if with_data:
        current, previous = bill_estimates([], 23, NOW)
        data = coord.LusData(ACCOUNT, current, previous, coord.WinterAverageData(2026, None, None), insights)
    return LusInsightSensor(SimpleNamespace(data=data), ACCOUNT, "USD", description)


def test_keys_are_unique_across_all_sensors():
    keys = [d.key for d in (*SENSORS, *INSIGHT_SENSORS)]
    assert len(keys) == len(set(keys))


def test_month_cost_breaks_down_by_utility(insights):
    sensor = make_sensor("month_cost", insights)
    attributes = sensor.extra_state_attributes
    assert sensor.unique_id == f"{ACCOUNT}_month_cost"
    assert sensor.native_unit_of_measurement == "USD"
    assert sensor.device_class == "monetary"
    assert attributes["period"] == "2026-10"
    assert sensor.native_value == attributes["total"]
    assert attributes["total"] == pytest.approx(
        attributes["electricity"] + attributes["water"] + attributes["wastewater"] + attributes["garbage"],
        abs=0.02,
    )
    assert attributes["kwh"] == 2.0 * 24 * 29
    assert attributes["data_through"] == "2026-10-29T23:59:59-05:00"
    assert len(attributes["months"]) == 13 and attributes["months"][-1]["period"] == "2026-10"
    assert attributes["billing_periods"][-2]["period"] == "2026-10-23"
    assert attributes["billing_periods"][-2]["complete"] is True


def test_year_cost_lists_each_year(insights):
    sensor = make_sensor("year_cost", insights)
    attributes = sensor.extra_state_attributes
    assert attributes["period"] == "2026"
    assert [y["period"] for y in attributes["years"]] == ["2025", "2026"]
    assert sensor.native_value == attributes["years"][-1]["total"]


def test_rate_sensors(insights):
    electric = make_sensor("electricity_rate", insights)
    # The 9/23-10/23 bill: 1,440 kWh at $0.09624 plus the $13 customer charge
    assert electric.native_value == round((1440 * 0.09624 + 13.00) / 1440, 5)
    assert electric.native_unit_of_measurement == "USD/kWh"
    assert electric.device_class is None
    attributes = electric.extra_state_attributes
    assert attributes["usage_only_rate"] == 0.09624
    assert attributes["period_end"] == "2026-10-23" and attributes["complete"] is True
    assert attributes["fixed_share"] == round(13.00 / (1440 * 0.09624 + 13.00), 3)

    water = make_sensor("water_rate", insights)
    used = 600.0 * 30
    cost = used * (0.00209 + 0.00747) + 6.99 + 1.00 + 11.29
    assert water.native_value == round(cost / used, 5)
    assert water.native_unit_of_measurement == "USD/gal"
    assert water.extra_state_attributes["per_1000_gallons"] == round(cost / used * 1000, 2)
    assert water.extra_state_attributes["gallons"] == 18000


def test_change_sensors(insights):
    month = make_sensor("cost_change_month", insights)
    assert month.native_unit_of_measurement == "%"
    attributes = month.extra_state_attributes
    assert (attributes["current_period"], attributes["previous_period"]) == ("2026-09", "2026-08")
    assert month.native_value == attributes["total"]["percent"]
    # September is a day shorter than August at the same daily usage.
    assert month.native_value < 0
    assert attributes["kwh"]["percent"] == pytest.approx(-3.2)
    assert attributes["month_to_date"]["current_period"] == "2026-10 to date"
    assert set(attributes["drivers"]) == {
        "electricity_usage", "electricity_rate", "water_usage", "water_rate", "fixed_charges",
    }

    year = make_sensor("cost_change_year", insights)
    assert year.native_value == 0.0
    assert year.extra_state_attributes["previous_period"] == "2025-09"
    # 2025 has no data before July, so the year so far can't be compared.
    assert year.extra_state_attributes["year_to_date"] is None


def test_forecast_sensors(insights):
    forecast = make_sensor("forecast_bill", insights)
    attributes = forecast.extra_state_attributes
    assert (attributes["period_start"], attributes["period_end"]) == ("2026-10-23", "2026-11-23")
    assert attributes["low"] <= forecast.native_value <= attributes["high"]
    assert attributes["method"] == "trend"
    assert forecast.native_value == pytest.approx(
        attributes["electricity"] + attributes["water"] + attributes["wastewater"] + attributes["garbage"],
        abs=0.02,
    )
    following = make_sensor("forecast_next_bill", insights)
    assert following.extra_state_attributes["period_start"] == "2026-11-23"
    assert following.extra_state_attributes["method"] == "seasonal"


def test_anomaly_sensor(insights):
    sensor = make_sensor("usage_anomalies", insights)
    assert sensor.native_value == 0
    assert sensor.native_unit_of_measurement is None
    assert sensor.extra_state_attributes["anomalies"] == []
    assert sensor.extra_state_attributes["latest_explanation"] is None

    ledger = an.build_ledger(
        [
            ("ELECTRICITY", series(START, END, lambda t: 2.0 + (20.0 if (t.date(), t.hour) == (date(2026, 10, 20), 18) else 0.0))),
            ("WATER", series(START, END, gallons)),
        ],
        RATES,
    )
    sensor = make_sensor("usage_anomalies", an.build_insights(ledger, RATES, NOW))
    assert sensor.native_value == 1
    attributes = sensor.extra_state_attributes
    assert attributes["latest_date"] == "2026-10-20"
    assert attributes["latest_cause"] == "short_heavy_load"
    assert attributes["excess_cost"] == round(20.0 * 0.09624, 2)
    assert attributes["anomalies"][0]["utility"] == "electricity"


def test_sensors_are_unknown_without_insights():
    for description in INSIGHT_SENSORS:
        for sensor in (
            make_sensor(description.key, None),
            make_sensor(description.key, None, with_data=False),
        ):
            assert sensor.native_value is None
            assert sensor.extra_state_attributes is None


def test_sensors_cope_with_an_empty_history():
    empty = an.build_insights(an.build_ledger([], RATES), RATES, NOW)
    for description in INSIGHT_SENSORS:
        sensor = make_sensor(description.key, empty)
        assert sensor.native_value in (None, 0)
        assert sensor.extra_state_attributes["data_through"] is None


def test_history_lists_stay_out_of_the_recorder():
    assert {"months", "billing_periods", "years", "anomalies"} <= LusInsightSensor._unrecorded_attributes


# --- Coordinator -------------------------------------------------------------


@pytest.fixture
def harness(monkeypatch):
    stored: dict[str, list[dict]] = {}
    queries: list = []

    class Recorder:
        async def async_add_executor_job(self, func, hass, start, end, ids, period, units, types):
            (stat_id,) = ids
            queries.append((stat_id, start, end, period, units, types))
            rows = [r for r in stored.get(stat_id, []) if start.timestamp() <= r["start"] < end.timestamp()]
            return {stat_id: rows} if rows else {}

    class Hass:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    monkeypatch.setattr(coord, "get_instance", lambda hass: Recorder())
    monkeypatch.setattr(coord.dt_util, "now", lambda tz=None: NOW)

    def make():
        c = object.__new__(coord.LusCoordinator)
        c.hass = Hass()
        c._history = {}
        c._winters = {}
        return c

    return SimpleNamespace(make=make, stored=stored, queries=queries)


def rows(hours: list[HourlyUsage]) -> list[dict]:
    total, out = 0.0, []
    for h in hours:
        total += h.value
        out.append({"start": h.start.timestamp(), "state": h.value, "sum": total})
    return out


async def test_insights_join_stored_history_to_the_fresh_hours(harness):
    fetched_from = datetime(2026, 9, 23, tzinfo=SITE_TZ)
    electric = series(START, END, lambda t: 2.0)
    water = series(START, END, gallons)
    harness.stored["lus:1000000001_electricity_consumption"] = rows(electric)
    harness.stored["lus:1000000002_water_consumption"] = rows(water)
    fresh = {
        ELECTRIC: [h for h in electric if h.start >= fetched_from],
        WATER: [h for h in water if h.start >= fetched_from],
    }

    c = harness.make()
    insights = await c._async_insights(fresh, {}, as_schedule(RATES))

    # Hours before the fetch came from statistics, in the meter's own units.
    stat_id, start, end, period, units, types = harness.queries[0]
    assert stat_id == "lus:1000000001_electricity_consumption"
    assert (end, period) == (fetched_from, "hour")
    assert units == {"energy": "kWh", "volume": "gal"}
    assert insights.months[0].label == "2025-07"
    assert insights.year_over_year is not None
    september = next(m for m in insights.months if m.label == "2026-09")
    assert september.kwh == pytest.approx(2.0 * 720) and september.complete

    # Later updates reuse the history instead of reading statistics again.
    harness.queries.clear()
    again = await c._async_insights(fresh, {}, as_schedule(RATES))
    assert harness.queries == []
    assert again.this_year.total == pytest.approx(insights.this_year.total)


async def test_stored_rows_without_a_state_use_the_running_sum(harness):
    electric = series(START, START + timedelta(hours=4), lambda t: 2.0)
    stored = rows(electric)
    for row in stored[1:]:
        row["state"] = None
    harness.stored["lus:1000000001_electricity_consumption"] = stored

    usage = await harness.make()._async_stored_usage(
        "lus:1000000001_electricity_consumption", START, START + timedelta(days=1)
    )
    assert list(usage.values()) == [2.0, 2.0, 2.0, 2.0]


async def test_history_is_trimmed_to_the_insight_window(harness):
    old = NOW - coord.INSIGHTS_HISTORY - timedelta(days=5)
    c = harness.make()
    c._history[ELECTRIC.uuid] = {old.astimezone(UTC): 9.0}
    await c._async_insights({ELECTRIC: series(END - timedelta(days=2), END, lambda t: 2.0)}, {}, as_schedule(RATES))
    assert min(c._history[ELECTRIC.uuid]) >= NOW - coord.INSIGHTS_HISTORY


async def test_a_failed_analysis_does_not_fail_the_update(harness, monkeypatch, caplog):
    def broken(*args):
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(coord, "_build_insights", broken)
    c = harness.make()
    fresh = {ELECTRIC: series(END - timedelta(days=2), END, lambda t: 2.0)}
    assert await c._async_insights(fresh, {}, as_schedule(RATES)) is None
    assert "Could not work out LUS spending insights" in caplog.text
