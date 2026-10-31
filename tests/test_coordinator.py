"""Tests for how the coordinator turns reads into statistics."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.lus import coordinator as coord
from custom_components.lus.api import Meter, Read
from custom_components.lus.cost import SITE_TZ, Rates, RateSchedule

CDT = timezone(timedelta(hours=-5))
METER = Meter("elec-uuid", "1000000001", "ELECTRICITY", "QUARTER_HOUR", date(2026, 10, 25))


class FakeClient:
    def __init__(self, reads: list[Read]) -> None:
        self.reads = reads
        self.requests: list[tuple[date, date]] = []
        self.periods: list[tuple[datetime, datetime]] = []
        self.bill_requests = 0

    async def async_get_bill_periods(self, meter):
        self.bill_requests += 1
        return self.periods

    async def async_get_reads(self, meter, start, end):
        self.requests.append((start, end))
        return [
            r for r in self.reads
            if start <= r.start.date() <= end
        ]


def quarter_reads(start: datetime, count: int) -> list[Read]:
    step = timedelta(minutes=15)
    return [Read(start + i * step, start + (i + 1) * step, 0.5) for i in range(count)]


@pytest.fixture
def harness(monkeypatch):
    added: list = []
    last_stats: dict = {}

    class Recorder:
        async def async_add_executor_job(self, func, hass, count, stat_id, *args):
            return {stat_id: last_stats[stat_id]} if stat_id in last_stats else {}

    monkeypatch.setattr(coord, "get_instance", lambda hass: Recorder())
    monkeypatch.setattr(
        coord, "async_add_external_statistics",
        lambda hass, meta, stats: added.append((meta, list(stats))),
    )
    monkeypatch.setattr(
        coord.dt_util, "now",
        lambda tz=None: datetime(2026, 10, 27, 12, tzinfo=CDT),
    )

    def make(reads):
        c = object.__new__(coord.LusCoordinator)
        c.hass = SimpleNamespace(config=SimpleNamespace(currency="USD"))
        c.client = FakeClient(reads)
        c._winters = {}
        c._bill_ends = {}
        c.cycles = None
        return c

    return SimpleNamespace(make=make, added=added, last_stats=last_stats)


async def test_first_import_backfills_from_first_read(harness):
    reads = quarter_reads(datetime(2026, 10, 25, tzinfo=CDT), 4 * 48)
    c = harness.make(reads)
    await c._async_import_meter(METER, Rates(), (None, 0.0))

    assert c.client.requests == [(date(2026, 10, 25), date(2026, 10, 27))]
    meta, stats = harness.added[0]
    assert meta["statistic_id"] == "lus:1000000001_electricity_consumption"
    assert meta["unit_of_measurement"] == "kWh"
    assert meta["has_sum"] is True
    assert len(stats) == 48
    assert stats[0]["state"] == pytest.approx(2.0)
    assert stats[-1]["sum"] == pytest.approx(96.0)


CONSUMPTION_ID = "lus:1000000001_electricity_consumption"
COST_ID = "lus:1000000001_electricity_cost"


async def test_later_import_continues_sum_after_last_hour(harness):
    reads = quarter_reads(datetime(2026, 10, 25, tzinfo=CDT), 4 * 48)
    last_hour = datetime(2026, 10, 26, 5, tzinfo=CDT)
    harness.last_stats[CONSUMPTION_ID] = [{"start": last_hour.timestamp(), "sum": 100.0}]
    harness.last_stats[COST_ID] = [{"start": last_hour.timestamp(), "sum": 10.0}]
    c = harness.make(reads)
    await c._async_import_meter(METER, Rates(), (None, 0.0))

    # Cost needs the whole billing cycle, which began 10/23.
    assert c.client.requests[0][0] == date(2026, 10, 23)
    (_, usage), (cost_meta, cost) = harness.added
    assert usage[0]["start"] == last_hour + timedelta(hours=1)
    assert len(usage) == 18
    assert usage[0]["sum"] == pytest.approx(102.0)
    assert usage[-1]["sum"] == pytest.approx(136.0)

    assert cost_meta["statistic_id"] == COST_ID
    assert cost_meta["unit_of_measurement"] == "USD"
    assert len(cost) == 18
    hourly_fixed = 13.00 / (31 * 24)  # 10/23-11/23 cycle
    assert cost[0]["state"] == pytest.approx(2.0 * 0.09624 + hourly_fixed)
    assert cost[0]["sum"] == pytest.approx(10.0 + cost[0]["state"])


async def test_cost_backfills_when_only_usage_exists(harness):
    # Upgrading from 0.1.0: usage history exists, cost history doesn't.
    reads = quarter_reads(datetime(2026, 10, 25, tzinfo=CDT), 4 * 48)
    last_hour = datetime(2026, 10, 26, 23, tzinfo=CDT)
    harness.last_stats[CONSUMPTION_ID] = [{"start": last_hour.timestamp(), "sum": 96.0}]
    c = harness.make(reads)
    await c._async_import_meter(METER, Rates(), (None, 0.0))

    assert c.client.requests[0][0] == date(2026, 10, 25)
    assert [meta["statistic_id"] for meta, _ in harness.added] == [COST_ID]
    assert len(harness.added[0][1]) == 48


async def test_nothing_new_adds_nothing(harness):
    reads = quarter_reads(datetime(2026, 10, 25, tzinfo=CDT), 4 * 24)
    last = [{"start": datetime(2026, 10, 25, 23, tzinfo=CDT).timestamp(), "sum": 48.0}]
    harness.last_stats[CONSUMPTION_ID] = last
    harness.last_stats[COST_ID] = last
    c = harness.make(reads)
    await c._async_import_meter(METER, Rates(), (None, 0.0))
    assert harness.added == []


async def test_wastewater_gets_usage_but_no_cost(harness):
    meter = Meter("ww-uuid", "1000000002", "WASTE_WATER", "HOUR", date(2026, 10, 25))
    reads = [
        Read(datetime(2026, 10, 25, h, tzinfo=CDT), datetime(2026, 10, 25, h, tzinfo=CDT) + timedelta(hours=1), 10.0)
        for h in range(23)
    ]
    c = harness.make(reads)
    assert await c._async_import_meter(meter, Rates(), (None, 0.0)) is None
    assert [meta["statistic_id"] for meta, _ in harness.added] == [
        "lus:1000000002_wastewater_consumption"
    ]


async def test_costs_cover_hours_the_meter_skipped(harness):
    meter = Meter("elec-uuid", "1000000001", "ELECTRICITY", "QUARTER_HOUR", date(2026, 10, 23))
    reads = quarter_reads(datetime(2026, 10, 23, tzinfo=CDT), 4 * 96)
    del reads[4 * 5:4 * 8]  # 10/23 05:00-07:59, long enough ago to be given up on
    c = harness.make(reads)
    await c._async_import_meter(meter, Rates(), (None, 0.0))

    (_, usage), (_, cost) = harness.added
    assert len(usage) == 93  # no usage for the skipped hours
    assert len(cost) == 96  # but they still carry the customer charge
    skipped = [
        s["state"] for s in cost
        if datetime(2026, 10, 23, 5, tzinfo=CDT) <= s["start"] < datetime(2026, 10, 23, 8, tzinfo=CDT)
    ]
    assert skipped == pytest.approx([13.00 / (31 * 24)] * 3)


def hourly_reads(start: datetime, end: datetime, value: float) -> list[Read]:
    """A read for every real hour from start to end, across DST changes."""
    reads = []
    t = start.astimezone(timezone.utc)
    while t < end:
        reads.append(
            Read(t.astimezone(SITE_TZ), (t + timedelta(hours=1)).astimezone(SITE_TZ), value)
        )
        t += timedelta(hours=1)
    return reads


WATER = Meter("water-uuid", "1000000002", "WATER", "HOUR", date(2025, 9, 23))

# The December-March bills' cycles, 11/23-3/23, in 30-day requests.
WINTER_REQUESTS = [
    (date(2025, 11, 23), date(2025, 12, 22)),
    (date(2025, 12, 23), date(2026, 1, 21)),
    (date(2026, 1, 22), date(2026, 2, 20)),
    (date(2026, 2, 21), date(2026, 3, 22)),
]


def water_imported_through(harness, last_hour: datetime) -> None:
    last = [{"start": last_hour.timestamp(), "sum": 0.0}]
    harness.last_stats["lus:1000000002_water_consumption"] = last
    harness.last_stats["lus:1000000002_water_cost"] = last


def winters(c) -> dict:
    """The kept winter averages, by meter and winter."""
    return {key[:2]: average for key, average in c._winters.items()}


def winter_requests(c) -> list[tuple[date, date]]:
    return [r for r in c.client.requests if r[0] < date(2026, 9, 1)]


def without_march_18_to_20(reads: list[Read]) -> list[Read]:
    return [r for r in reads if not date(2026, 3, 18) <= r.start.date() <= date(2026, 3, 20)]


async def test_automatic_tier_limit_reads_last_winter_once(harness):
    # 30 gal an hour last winter, 400 this fall.
    reads = hourly_reads(
        datetime(2025, 11, 23, tzinfo=SITE_TZ), datetime(2026, 3, 23, tzinfo=SITE_TZ), 30.0
    ) + hourly_reads(
        datetime(2026, 9, 23, tzinfo=SITE_TZ), datetime(2026, 10, 27, 12, tzinfo=SITE_TZ), 400.0
    )
    last_hour = datetime(2026, 10, 26, 23, tzinfo=CDT)
    water_imported_through(harness, last_hour)
    c = harness.make(reads)
    await c._async_import_meter(WATER, Rates(), (last_hour, 0.0))
    await c._async_import_meter(WATER, Rates(), (last_hour, 0.0))

    assert winter_requests(c) == WINTER_REQUESTS  # the first time only
    limit = 30 * (720 + 744 + 744 + 671) / 4  # March's cycle loses an hour to DST
    assert winters(c) == {("water-uuid", 2026): pytest.approx(limit)}
    # By 10/27 this cycle is past the limit / 0.75: tier 2, wastewater on 75%.
    _, cost = harness.added[1]
    hourly_fixed = (6.99 + 1.00 + 11.29) / (31 * 24)
    assert cost[0]["state"] == pytest.approx(400 * 0.00331 + 0.75 * 400 * 0.00747 + hourly_fixed)


async def test_costs_wait_while_last_winters_reads_come_in(harness, monkeypatch):
    # 3/26: March's bill is missing 3/18-3/20, which may still arrive.
    monkeypatch.setattr(
        coord.dt_util, "now", lambda tz=None: datetime(2026, 3, 26, 12, tzinfo=SITE_TZ)
    )
    reads = without_march_18_to_20(
        hourly_reads(datetime(2025, 11, 23, tzinfo=SITE_TZ), datetime(2026, 3, 26, tzinfo=SITE_TZ), 30.0)
    )
    last_hour = datetime(2026, 3, 17, 23, tzinfo=SITE_TZ)
    water_imported_through(harness, last_hour)
    c = harness.make(reads)
    await c._async_import_meter(WATER, Rates(), (last_hour, 0.0))

    april_bill = datetime(2026, 3, 23, tzinfo=SITE_TZ)
    (_, usage), (_, cost) = harness.added
    assert usage[-1]["start"] > april_bill
    assert cost[-1]["start"] < april_bill
    assert c._winters == {}


async def test_an_incomplete_winter_is_used_but_read_again(harness):
    # March's bill never gets 3/18-3/20.
    reads = without_march_18_to_20(
        hourly_reads(datetime(2025, 11, 23, tzinfo=SITE_TZ), datetime(2026, 3, 23, tzinfo=SITE_TZ), 30.0)
    ) + hourly_reads(
        datetime(2026, 9, 23, tzinfo=SITE_TZ), datetime(2026, 10, 27, 12, tzinfo=SITE_TZ), 30.0
    )
    last_hour = datetime(2026, 10, 26, 23, tzinfo=CDT)
    water_imported_through(harness, last_hour)
    c = harness.make(reads)
    await c._async_import_meter(WATER, Rates(), (last_hour, 0.0))
    await c._async_import_meter(WATER, Rates(), (last_hour, 0.0))

    assert winter_requests(c) == WINTER_REQUESTS * 2
    assert c._winters == {}


async def test_a_winter_partly_before_the_first_read_averages_the_rest(harness):
    meter = Meter("water-uuid", "1000000002", "WATER", "HOUR", date(2026, 1, 5))
    reads = hourly_reads(
        datetime(2026, 1, 5, tzinfo=SITE_TZ), datetime(2026, 3, 23, tzinfo=SITE_TZ), 30.0
    ) + hourly_reads(
        datetime(2026, 9, 23, tzinfo=SITE_TZ), datetime(2026, 10, 27, 12, tzinfo=SITE_TZ), 30.0
    )
    last_hour = datetime(2026, 10, 26, 23, tzinfo=CDT)
    water_imported_through(harness, last_hour)
    c = harness.make(reads)
    await c._async_import_meter(meter, Rates(), (last_hour, 0.0))

    # Only February's and March's bills were metered from the start of their cycles.
    assert winter_requests(c) == [
        (date(2026, 1, 23), date(2026, 2, 21)),
        (date(2026, 2, 22), date(2026, 3, 22)),
    ]
    assert winters(c) == {("water-uuid", 2026): pytest.approx(30 * (744 + 671) / 2)}


async def test_no_winter_reads_are_requested_before_the_first_read(harness):
    meter = Meter("water-uuid", "1000000002", "WATER", "HOUR", date(2026, 9, 23))
    reads = hourly_reads(
        datetime(2026, 9, 23, tzinfo=SITE_TZ), datetime(2026, 10, 27, 12, tzinfo=SITE_TZ), 30.0
    )
    c = harness.make(reads)
    await c._async_import_meter(meter, Rates(), (None, 0.0))

    assert all(start >= date(2026, 9, 23) for start, _ in c.client.requests)
    assert winters(c) == {("water-uuid", 2026): None}


# Made-up bill periods on varying days.
BILLS = [
    (datetime(2026, 7, 22, tzinfo=SITE_TZ), datetime(2026, 8, 21, tzinfo=SITE_TZ)),
    (datetime(2026, 8, 21, tzinfo=SITE_TZ), datetime(2026, 9, 23, tzinfo=SITE_TZ)),
    (datetime(2026, 9, 23, tzinfo=SITE_TZ), datetime(2026, 10, 22, tzinfo=SITE_TZ)),
]


async def test_meter_cycles_follow_its_bills_and_recost_the_cycle_a_bill_closes(harness):
    c = harness.make([])
    c.client.periods = BILLS[:2]
    schedule, end, redo = await c._async_meter_rates(METER, RateSchedule())
    assert schedule.cycles.bounds(datetime(2026, 8, 25, tzinfo=SITE_TZ)) == BILLS[1]
    # The first look since starting costs the newest bill's cycle again.
    assert (end, redo) == (BILLS[1][1], BILLS[1][0])

    c._bill_ends[METER.uuid] = end
    _, _, redo = await c._async_meter_rates(METER, RateSchedule())
    assert redo is None  # no new bill

    c.client.periods = BILLS  # the next bill closes the cycle that was open
    _, end, redo = await c._async_meter_rates(METER, RateSchedule())
    assert (end, redo) == (BILLS[2][1], BILLS[2][0])


async def test_a_day_in_the_options_skips_the_bill_dates(harness):
    c = harness.make([])
    c.client.periods = BILLS
    schedule, end, redo = await c._async_meter_rates(
        METER, RateSchedule([(None, Rates(billing_day=10))])
    )
    assert (end, redo, c.client.bill_requests) == (None, None, 0)
    assert schedule.cycles.bounds(datetime(2026, 8, 25, tzinfo=SITE_TZ))[0].day == 10
