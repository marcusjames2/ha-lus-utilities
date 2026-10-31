"""Tests for dated rates and recalculating stored costs."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.lus import (
    config_flow as flow_mod,
    coordinator as coord,
    migrate_options_1_2,
    migrate_options_1_3,
    migrate_options_1_4,
    migrate_options_1_5,
)
from custom_components.lus.api import HourlyUsage, Meter, Read
from custom_components.lus.cost import (
    SITE_TZ,
    BillingCycles,
    Rates,
    RateSchedule,
    electricity_costs,
    water_costs,
)

CDT = timezone(timedelta(hours=-5))
SEPT = Rates(electric_fuel_adjustment_rate=0.050)
OCT = Rates()  # fuel adjustment 0.044
SCHEDULE = RateSchedule([(None, SEPT), (date(2026, 10, 23), OCT)])


def local(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=SITE_TZ)


def test_rates_apply_from_their_date_until_the_next():
    assert SCHEDULE.at(local(2026, 10, 22, 23)) == SEPT
    assert SCHEDULE.at(local(2026, 10, 23)) == OCT
    assert SCHEDULE.at(local(2027, 1, 1)) == OCT


def test_undated_rates_cover_history_before_the_first_date():
    schedule = RateSchedule([(date(2026, 9, 23), SEPT)])
    assert schedule.at(local(2025, 1, 1)) == SEPT


def test_with_rates_replaces_same_date_and_keeps_later_sets():
    nov = Rates(electric_fuel_adjustment_rate=0.040)
    schedule = SCHEDULE.with_rates(date(2026, 9, 23), nov)
    assert [d for d, _ in schedule.periods] == [None, date(2026, 9, 23), date(2026, 10, 23)]
    assert schedule.at(local(2026, 10, 1)) == nov
    assert schedule.at(local(2026, 10, 24)) == OCT

    replaced = schedule.with_rates(date(2026, 10, 23), nov)
    assert replaced.at(local(2026, 10, 24)) == nov
    assert len(replaced.periods) == 3


def test_flat_02x_options_become_one_undated_set():
    schedule = RateSchedule.from_options({"electric_fuel_adjustment_rate": 0.05, "billing_day": 20})
    assert schedule.periods == [(None, Rates(electric_fuel_adjustment_rate=0.05, billing_day=20))]


def test_schedule_round_trips_through_options():
    restored = RateSchedule.from_options({"rate_schedule": SCHEDULE.as_options()})
    assert restored.periods == SCHEDULE.periods


def test_upgrade_moves_guessed_summer_months_and_recalculates_everything():
    guessed = Rates(wastewater_summer_months="5,6,7,8,9,10")
    chosen = Rates(wastewater_summer_months="6,7,8")
    options = RateSchedule([(None, guessed), (date(2026, 9, 23), chosen)]).as_options()
    migrated = migrate_options_1_2({"rate_schedule": options})

    assert [e["wastewater_summer_months"] for e in migrated["rate_schedule"]] == [
        "4,5,6,7,8,9,10,11",
        "6,7,8",
    ]
    assert migrated["recalculate_from"] == "2000-01-01"
    # 0.2.x's flat options, and options never saved.
    flat = migrate_options_1_2({"wastewater_summer_months": "5,6,7,8,9,10"})
    assert flat["wastewater_summer_months"] == "4,5,6,7,8,9,10,11"
    assert migrate_options_1_2({}) == {"recalculate_from": "2000-01-01"}


def test_upgrade_to_the_automatic_limit_recalculates_only_unsaved_rates():
    saved = {"rate_schedule": SCHEDULE.as_options()}
    assert migrate_options_1_3(saved) == saved
    assert migrate_options_1_3({"water_tier1_gallons": 20470}) == {"water_tier1_gallons": 20470}
    assert migrate_options_1_3({}) == {"recalculate_from": "2000-01-01"}


def test_upgrade_to_costs_covering_skipped_hours_recalculates_everything():
    saved = {"rate_schedule": SCHEDULE.as_options()}
    assert migrate_options_1_4(saved) == {**saved, "recalculate_from": "2000-01-01"}


def test_upgrade_to_bill_dates_moves_the_old_default_day_and_recalculates():
    old_default = Rates(billing_day=23)
    chosen = Rates(billing_day=10)
    options = RateSchedule([(None, old_default), (date(2026, 9, 23), chosen)]).as_options()
    migrated = migrate_options_1_5({"rate_schedule": options})

    assert [e["billing_day"] for e in migrated["rate_schedule"]] == [0, 10]
    assert migrated["recalculate_from"] == "2000-01-01"
    assert migrate_options_1_5({"billing_day": 23})["billing_day"] == 0  # 0.2.x


def test_costs_switch_rates_at_the_effective_date():
    hours = [
        HourlyUsage(local(2026, 10, 22, 23), 1.0),
        HourlyUsage(local(2026, 10, 23, 0), 1.0),
    ]
    before, after = electricity_costs(hours, SCHEDULE)
    assert before.value - after.value == pytest.approx(0.050 - 0.044, abs=1e-3)


def test_water_tier_limit_follows_the_schedule():
    # November is a summer month, so water is tiered; no wastewater charge, so
    # only the water tiers differ.
    small_tier = Rates(water_tier1_gallons=10, wastewater_rate=0)
    schedule = RateSchedule([(None, Rates()), (date(2026, 10, 23), small_tier)])
    hours = [HourlyUsage(local(2026, 10, 23, h), 10.0) for h in range(2)]
    first, second = water_costs(hours, schedule)
    # The second hour is all tier 2 under the new limit.
    assert second.value - first.value == pytest.approx(10 * (0.00331 - 0.00209))


# --- Recalculation in the coordinator -------------------------------------

METER = Meter("elec-uuid", "1000000001", "ELECTRICITY", "QUARTER_HOUR", date(2026, 10, 1))
CONSUMPTION_ID = "lus:1000000001_electricity_consumption"
COST_ID = "lus:1000000001_electricity_cost"


def quarter_reads(start: datetime, count: int) -> list[Read]:
    step = timedelta(minutes=15)
    return [Read(start + i * step, start + (i + 1) * step, 0.5) for i in range(count)]


class FakeClient:
    def __init__(self, reads):
        self.reads = reads
        self.requests = []

    async def async_get_reads(self, meter, start, end):
        self.requests.append((start, end))
        return [r for r in self.reads if start <= r.start.date() <= end]

    async def async_get_bill_periods(self, meter):
        return self.periods

    periods: list = []


@pytest.fixture
def harness(monkeypatch):
    added = []
    last = {}       # stat id -> row returned by get_last_statistics
    history = {}    # stat id -> rows returned by statistics_during_period
    queries = []

    class Recorder:
        async def async_add_executor_job(self, func, hass, *args):
            if func is coord.statistics_during_period:
                start, end, ids = args[0], args[1], args[2]
                (stat_id,) = ids
                queries.append((stat_id, start, end))
                rows = [r for r in history.get(stat_id, []) if start.timestamp() <= r["start"] < end.timestamp()]
                return {stat_id: rows} if rows else {}
            stat_id = args[1]
            return {stat_id: last[stat_id]} if stat_id in last else {}

    monkeypatch.setattr(coord, "get_instance", lambda hass: Recorder())
    monkeypatch.setattr(
        coord, "async_add_external_statistics",
        lambda hass, meta, stats: added.append((meta["statistic_id"], list(stats))),
    )
    monkeypatch.setattr(coord.dt_util, "now", lambda tz=None: datetime(2026, 10, 27, 12, tzinfo=CDT))

    def make(reads):
        c = object.__new__(coord.LusCoordinator)
        c.hass = SimpleNamespace(config=SimpleNamespace(currency="USD"))
        c.client = FakeClient(reads)
        c._winters = {}
        c._bill_ends = {}
        c.cycles = None
        return c

    return SimpleNamespace(make=make, added=added, last=last, history=history, queries=queries)


async def test_recalculation_overwrites_costs_from_the_date(harness):
    reads = quarter_reads(datetime(2026, 10, 20, tzinfo=CDT), 4 * 24 * 6)  # 10/20-10/25
    newest = datetime(2026, 10, 25, 23, tzinfo=CDT).timestamp()
    harness.last[CONSUMPTION_ID] = [{"start": newest, "sum": 1000.0}]
    harness.last[COST_ID] = [{"start": newest, "sum": 99.0}]
    # Stored cost for the hour before the effective date
    before = datetime(2026, 10, 22, 23, tzinfo=CDT).timestamp()
    harness.history[COST_ID] = [{"start": before, "sum": 50.0}]

    c = harness.make(reads)
    recalc = local(2026, 10, 23)
    await c._async_import_meter(METER, SCHEDULE, (None, 0.0), recalc)

    assert harness.queries[0][0] == COST_ID and harness.queries[0][2] == recalc
    # Usage is already stored and untouched; cost is rewritten from 10/23 on.
    assert [stat_id for stat_id, _ in harness.added] == [COST_ID]
    cost = harness.added[0][1]
    assert cost[0]["start"] == recalc
    assert len(cost) == 3 * 24
    hourly_fixed = 13.00 / (31 * 24)  # 10/23-11/23 cycle
    assert cost[0]["state"] == pytest.approx(2.0 * 0.09624 + hourly_fixed)
    assert cost[0]["sum"] == pytest.approx(50.0 + cost[0]["state"])


async def test_recalculation_before_any_stored_cost_rebuilds_everything(harness):
    reads = quarter_reads(datetime(2026, 10, 20, tzinfo=CDT), 4 * 24 * 2)
    newest = datetime(2026, 10, 21, 23, tzinfo=CDT).timestamp()
    harness.last[CONSUMPTION_ID] = [{"start": newest, "sum": 96.0}]
    harness.last[COST_ID] = [{"start": newest, "sum": 10.0}]

    c = harness.make(reads)
    await c._async_import_meter(METER, SCHEDULE, (None, 0.0), local(2026, 9, 1))

    assert c.client.requests[0][0] == METER.first_read
    (stat_id, cost), = harness.added
    assert stat_id == COST_ID
    assert len(cost) == 48
    assert cost[0]["sum"] == pytest.approx(cost[0]["state"])


async def test_update_clears_the_recalculation_date(harness, monkeypatch):
    harness.last[CONSUMPTION_ID] = [{"start": datetime(2026, 10, 25, tzinfo=CDT).timestamp(), "sum": 1.0}]
    c = harness.make([])
    updated = {}

    class Entries:
        def async_update_entry(self, entry, options):
            updated.update(options)

    c.hass.config_entries = Entries()
    c.config_entry = SimpleNamespace(
        options={"rate_schedule": SCHEDULE.as_options(), "recalculate_from": "2026-10-23"}
    )

    async def no_op(*args):
        return None

    c.client.async_login = no_op
    c.client.account_id = "5000000000"

    async def meters():
        return [METER]

    c.client.async_get_meters = meters
    await c._async_update_data()

    assert "recalculate_from" not in updated
    assert updated["rate_schedule"] == SCHEDULE.as_options()


# Made-up bill periods, the last closing on 10/22.
BILLS = [
    (local(2026, 8, 21), local(2026, 9, 24)),
    (local(2026, 9, 24), local(2026, 10, 22)),
]


def update_harness(harness, reads, fail=False):
    """A coordinator whose update runs against the fake client and recorder."""
    c = harness.make(reads)
    c.client.periods = BILLS
    updated = {}

    class Entries:
        def async_update_entry(self, entry, options):
            updated.update(options)

    c.hass.config_entries = Entries()
    c.config_entry = SimpleNamespace(options={"rate_schedule": SCHEDULE.as_options()})

    async def no_op(*args):
        return None

    async def meters():
        return [METER]

    async def broken(*args):
        raise coord.LusError("portal down")

    c.client.async_login = no_op
    c.client.account_id = "5000000000"
    c.client.async_get_meters = meters
    if fail:
        c.client.async_get_reads = broken
    return c, updated


async def test_update_recosts_the_cycle_the_newest_bill_closed(harness):
    harness.last[CONSUMPTION_ID] = [{"start": datetime(2026, 10, 25, tzinfo=CDT).timestamp(), "sum": 1.0}]
    c, updated = update_harness(harness, [])
    await c._async_update_data()

    # Cost and total were looked up from the start of the cycle the bill closed.
    assert {stat_id for stat_id, _, end in harness.queries if end == BILLS[1][0]} == {
        COST_ID, "lus:5000000000_total_cost"
    }
    assert c._bill_ends == {METER.uuid: BILLS[1][1]}
    assert c.cycles.bounds(local(2026, 10, 1)) == BILLS[1]
    assert updated == {}  # nothing to clear from the options


async def test_update_costs_each_hour_in_its_bill_period(harness):
    # By the bills, 10/20 is in the 9/24-10/22 period: 28 days, not 9/23-10/23's 30.
    reads = quarter_reads(datetime(2026, 10, 20, tzinfo=CDT), 4 * 24 * 3)
    c, _ = update_harness(harness, reads)
    await c._async_update_data()

    cost = next(stats for stat_id, stats in harness.added if stat_id == COST_ID)
    noon = next(s for s in cost if s["start"] == datetime(2026, 10, 20, 12, tzinfo=CDT))
    assert noon["state"] == pytest.approx(2.0 * (0.05224 + 0.050) + 13.00 / (28 * 24))


async def test_a_failed_update_doesnt_remember_the_bill(harness):
    c, _ = update_harness(harness, [], fail=True)
    with pytest.raises(coord.UpdateFailed):
        await c._async_update_data()
    assert c._bill_ends == {}  # so the next update costs that cycle again


# --- Options flow ----------------------------------------------------------


class Flow(flow_mod.LusOptionsFlow):
    options: dict = {}
    runtime_data = None

    @property
    def config_entry(self):
        return SimpleNamespace(options=self.options, runtime_data=self.runtime_data)


def make_flow(options):
    flow = Flow()
    flow.options = options
    flow.hass = SimpleNamespace()
    return flow


async def test_flow_defaults_to_the_latest_closed_cycle(monkeypatch):
    monkeypatch.setattr(flow_mod.dt_util, "now", lambda tz=None: local(2026, 10, 31, 9))
    result = await make_flow({}).async_step_init()
    (field,) = result["data_schema"].schema
    assert field.default() == "2026-09-23"


async def test_flow_defaults_to_the_last_bill_period(monkeypatch):
    monkeypatch.setattr(flow_mod.dt_util, "now", lambda tz=None: local(2026, 10, 31, 9))
    flow = make_flow({})
    flow.runtime_data = SimpleNamespace(cycles=BillingCycles(BILLS))
    result = await flow.async_step_init()
    (field,) = result["data_schema"].schema
    assert field.default() == "2026-09-24"


async def test_flow_saves_dated_rates_and_requests_recalculation(monkeypatch):
    flow = make_flow({"rate_schedule": SCHEDULE.as_options()})
    form = await flow.async_step_init({"effective_from": "2026-09-23"})
    assert form["step_id"] == "rates"
    defaults = {k.schema: k.default() for k in form["data_schema"].schema}
    # Pre-filled with what was in effect on 9/23 (the undated September set)
    assert defaults["electric_fuel_adjustment_rate"] == 0.050

    result = await flow.async_step_rates({**defaults, "electric_fuel_adjustment_rate": 0.047})
    saved = RateSchedule.from_options(result["data"])
    assert saved.at(local(2026, 10, 1)).electric_fuel_adjustment_rate == 0.047
    assert saved.at(local(2026, 10, 24)) == OCT
    assert result["data"]["recalculate_from"] == "2026-09-23"


async def test_flow_keeps_the_earliest_pending_recalculation():
    flow = make_flow({"rate_schedule": SCHEDULE.as_options(), "recalculate_from": "2026-08-23"})
    form = await flow.async_step_init({"effective_from": "2026-10-23"})
    defaults = {k.schema: k.default() for k in form["data_schema"].schema}
    result = await flow.async_step_rates(defaults)
    assert result["data"]["recalculate_from"] == "2026-08-23"
