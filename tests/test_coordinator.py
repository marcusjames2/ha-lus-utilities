"""Tests for how the coordinator turns reads into statistics."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.lus import coordinator as coord
from custom_components.lus.api import Meter, Read

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
        async def async_add_executor_job(self, func, *args):
            return last_stats

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
        c.hass = SimpleNamespace()
        c.client = FakeClient(reads)
        return c

    return SimpleNamespace(make=make, added=added, last_stats=last_stats)


async def test_first_import_backfills_from_first_read(harness):
    reads = quarter_reads(datetime(2026, 10, 25, tzinfo=CDT), 4 * 48)
    c = harness.make(reads)
    await c._async_import_meter(METER)

    assert c.client.requests == [(date(2026, 10, 25), date(2026, 10, 27))]
    meta, stats = harness.added[0]
    assert meta["statistic_id"] == "lus:1000000001_electricity_consumption"
    assert meta["unit_of_measurement"] == "kWh"
    assert meta["has_sum"] is True
    assert len(stats) == 48
    assert stats[0]["state"] == pytest.approx(2.0)
    assert stats[-1]["sum"] == pytest.approx(96.0)


async def test_later_import_continues_sum_after_last_hour(harness):
    reads = quarter_reads(datetime(2026, 10, 25, tzinfo=CDT), 4 * 48)
    last_hour = datetime(2026, 10, 26, 5, tzinfo=CDT)
    harness.last_stats["lus:1000000001_electricity_consumption"] = [
        {"start": last_hour.timestamp(), "sum": 100.0}
    ]
    c = harness.make(reads)
    await c._async_import_meter(METER)

    # Re-reads from the day of the last saved hour, local time.
    assert c.client.requests[0][0] == date(2026, 10, 26)
    _, stats = harness.added[0]
    assert stats[0]["start"] == last_hour + timedelta(hours=1)
    assert len(stats) == 18
    assert stats[0]["sum"] == pytest.approx(102.0)
    assert stats[-1]["sum"] == pytest.approx(136.0)


async def test_nothing_new_adds_nothing(harness):
    reads = quarter_reads(datetime(2026, 10, 25, tzinfo=CDT), 4 * 24)
    harness.last_stats["lus:1000000001_electricity_consumption"] = [
        {"start": datetime(2026, 10, 25, 23, tzinfo=CDT).timestamp(), "sum": 48.0}
    ]
    c = harness.make(reads)
    await c._async_import_meter(METER)
    assert harness.added == []
