"""Set up the integration in Home Assistant against a fake portal."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lus.api import Meter, Read
from custom_components.lus.const import DOMAIN
from custom_components.lus.cost import SITE_TZ, Rates

CDT = timezone(timedelta(hours=-5))
ACCOUNT = "5000000000"
METERS = [
    Meter("elec-uuid", "1000000001", "ELECTRICITY", "QUARTER_HOUR", date(2026, 9, 23)),
    # A year earlier, so last winter's bills set the water tier-1 limit.
    Meter("water-uuid", "1000000002", "WATER", "HOUR", date(2025, 9, 23)),
]


class FakeClient:
    """Every meter reports a steady flow from its first read through 10/29/2026."""

    account_id = ACCOUNT

    def __init__(self, *args) -> None:
        pass

    async def async_login(self) -> None:
        pass

    async def async_get_meters(self):
        return METERS

    async def async_get_bill_periods(self, meter):
        # Bills read on the 23rd: each period starts at midnight on the 24th.
        periods, start = [], date(meter.first_read.year, meter.first_read.month, 24)
        while start < date(2026, 10, 24):
            end = date(start.year + start.month // 12, start.month % 12 + 1, 24)
            periods.append((datetime(start.year, start.month, 24, tzinfo=SITE_TZ),
                            datetime(end.year, end.month, 24, tzinfo=SITE_TZ)))
            start = end
        return periods

    async def async_get_reads(self, meter, start, end):
        step = timedelta(minutes=15) if meter.reads_per_hour == 4 else timedelta(hours=1)
        value = 0.5 if meter.reads_per_hour == 4 else 30.0
        t = datetime(start.year, start.month, start.day, tzinfo=CDT)
        last = min(datetime(end.year, end.month, end.day, tzinfo=CDT) + timedelta(days=1),
                   datetime(2026, 10, 30, tzinfo=CDT))
        reads = []
        while t < last:
            reads.append(Read(t, t + step, value))
            t += step
        return reads


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN, unique_id=ACCOUNT, title=f"LUS account {ACCOUNT}",
        data={"username": "user", "password": "pw"},
    )
    e.add_to_hass(hass)
    return e


async def test_setup_creates_bill_sensors_and_statistics(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-10-31T14:00:00+00:00")
    hass.config.currency = "USD"
    with patch("custom_components.lus.coordinator.LusClient", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, ACCOUNT)})
    assert device is not None and entry.entry_id in device.config_entries and device.name == "LUS"

    ids = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    insight_sensors = {
        "month_cost", "year_cost", "electricity_rate", "water_rate", "cost_change_month",
        "cost_change_year", "forecast_bill", "forecast_next_bill", "usage_anomalies",
    }
    assert set(ids) == {
        f"{ACCOUNT}_current_bill",
        f"{ACCOUNT}_projected_bill",
        f"{ACCOUNT}_projected_bill_change",
        f"{ACCOUNT}_last_bill",
        f"{ACCOUNT}_winter_water_average",
        *(f"{ACCOUNT}_{key}" for key in insight_sensors),
    }

    current = hass.states.get(ids[f"{ACCOUNT}_current_bill"])
    projected = hass.states.get(ids[f"{ACCOUNT}_projected_bill"])
    change_id = ids[f"{ACCOUNT}_projected_bill_change"]
    change = hass.states.get(change_id)
    last = hass.states.get(ids[f"{ACCOUNT}_last_bill"])
    assert current.attributes["unit_of_measurement"] == "USD"
    assert current.attributes["friendly_name"] == "LUS Estimated bill this period"
    assert current.attributes["period_start"] == "2026-10-24"
    assert last.attributes["period_start"] == "2026-09-24"
    assert last.attributes["complete"] is True
    assert change.attributes["friendly_name"] == "LUS Projected change from last bill"
    assert change.attributes["device_class"] == "monetary"
    assert change.attributes["unit_of_measurement"] == "USD"
    # The display precision is kept on the registry entry, not in the state.
    registered = er.async_get(hass).async_get(change_id)
    assert registered.options["sensor"]["suggested_display_precision"] == 2
    assert registered.device_id == device.id

    winter = hass.states.get(ids[f"{ACCOUNT}_winter_water_average"])
    assert winter.attributes["unit_of_measurement"] == "gal"
    assert winter.attributes["friendly_name"] == "LUS Winter water average"
    assert winter.attributes["winter_year"] == 2026
    assert winter.attributes["water_meter_id"] == "1000000002"
    assert float(winter.state) == pytest.approx(30 * (720 + 744 + 744 + 671) / 4)

    # Default rates, 9/24-10/24 by the bills: electric 2 kWh/h and water 30 gal/h for 30 days.
    # The water tier-1 limit is the meter's December-March average: 30 gal for
    # each hour of those bills' cycles, March's losing one to DST.
    kwh, gallons = 2 * 24 * 30, 30 * 24 * 30
    limit = 30 * (720 + 744 + 744 + 671) / 4
    expected = (
        kwh * 0.09624 + 13.00
        + limit * 0.00209 + (gallons - limit) * 0.00331
        + limit * 0.00747  # October is a summer-discount month
        + 6.99 + 1.00 + 11.29 + 38.04
    )
    assert float(last.state) == pytest.approx(expected, abs=0.01)
    assert float(current.state) > 0
    assert float(change.state) == pytest.approx(
        float(projected.state) - float(last.state), abs=0.01
    )

    # Spending insights, from the same usage, rates and bill periods
    def state(key: str):
        return hass.states.get(ids[f"{ACCOUNT}_{key}"])

    month = state("month_cost")
    assert month.attributes["friendly_name"] == "LUS Cost this month"
    assert month.attributes["unit_of_measurement"] == "USD"
    assert month.attributes["period"] == "2026-10"
    assert float(month.state) == pytest.approx(
        sum(month.attributes[u] for u in ("electricity", "water", "wastewater", "garbage")),
        abs=0.02,
    )
    # The last closed bill, 9/24-10/24, by the bills' own dates.
    assert month.attributes["billing_periods"][-2]["period"] == "2026-10-24"
    assert month.attributes["billing_periods"][-2]["total"] == pytest.approx(expected, abs=0.01)
    rate = state("electricity_rate")
    assert rate.attributes["unit_of_measurement"] == "USD/kWh"
    assert float(rate.state) == pytest.approx((kwh * 0.09624 + 13.00) / kwh, abs=1e-5)
    assert state("water_rate").attributes["unit_of_measurement"] == "USD/gal"
    forecast = state("forecast_bill")
    assert forecast.attributes["period_end"] == "2026-11-24"
    assert float(forecast.state) > float(current.state)
    # Electricity has a month of history: too little to compare with a year ago.
    assert state("cost_change_year").state == "unknown"
    # 30 gal every hour of every day is what a leak looks like.
    assert state("usage_anomalies").attributes["latest_cause"] == "continuous_flow"

    await async_wait_recording_done(hass)
    stats = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.parse_datetime("2026-09-01T00:00:00+00:00"),
        None,
        {f"{DOMAIN}:{ACCOUNT}_total_cost"},
        "month",
        None,
        {"change"},
    )
    assert stats[f"{DOMAIN}:{ACCOUNT}_total_cost"]


async def test_upgrade_from_040_moves_summer_months_and_recalculates(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-10-31T14:00:00+00:00")
    hass.config.currency = "USD"
    saved = {"effective_from": None, **Rates(wastewater_summer_months="5,6,7,8,9,10").as_options()}
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ACCOUNT, title=f"LUS account {ACCOUNT}",
        data={"username": "user", "password": "pw"},
        options={"rate_schedule": [saved]}, minor_version=1,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.lus.coordinator.LusClient", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 5
    assert entry.options["rate_schedule"][0]["wastewater_summer_months"] == "4,5,6,7,8,9,10,11"
    # The first update recomputed every stored cost and cleared the request.
    assert "recalculate_from" not in entry.options


async def test_history_is_read_back_from_statistics_after_a_restart(
    recorder_mock: Recorder, hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-10-31T14:00:00+00:00")
    hass.config.currency = "USD"
    with patch("custom_components.lus.coordinator.LusClient", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
        first = entry.runtime_data.data.insights

        # Three weeks on, a fresh coordinator only refetches from the previous
        # billing cycle; September has to come from the stored statistics.
        freezer.move_to("2026-11-25T14:00:00+00:00")
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    again = entry.runtime_data.data.insights

    def month(insights, label):
        return next(m for m in insights.months if m.label == label)

    # Water has a year of history; nothing before the refetched cycles was
    # read from the portal again, so it can only have come from statistics.
    assert again.months[0].label == first.months[0].label == "2025-09"
    for label in ("2026-01", "2026-06"):
        assert month(again, label).gallons == pytest.approx(month(first, label).gallons)
    assert month(again, "2026-01").gallons == pytest.approx(30.0 * 24 * 31)
    assert month(again, "2026-10").kwh == pytest.approx(month(first, "2026-10").kwh)


async def test_failed_sign_in_does_not_put_the_password_back_in_the_form(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    from homeassistant.config_entries import SOURCE_USER

    from custom_components.lus.api import LusAuthError

    class Rejecting(FakeClient):
        async def async_login(self) -> None:
            raise LusAuthError("Login rejected")

    with patch("custom_components.lus.config_flow.LusClient", Rejecting):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "someone", "password": "hunter2"}
        )

    assert result["errors"] == {"base": "invalid_auth"}
    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested == {"username": "someone", "password": None}
