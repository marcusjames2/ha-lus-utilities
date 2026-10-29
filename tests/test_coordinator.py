"""Tests for how the coordinator turns reads into statistics."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.lus import coordinator as coord
from custom_components.lus.api import Meter, Read
from custom_components.lus.cost import Rates

CDT = timezone(timedelta(hours=-5))
METER = Meter("elec-uuid", "1000000001", "ELECTRICITY", "QUARTER_HOUR", date(2026, 10, 25))


class FakeClient:
    def __init__(self, reads: list[Read]) -> None:
        self.reads = reads
        self.requests: list[tuple[date, date]] = []

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
