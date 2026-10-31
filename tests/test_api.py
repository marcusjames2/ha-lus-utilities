"""Offline tests for the LUS client and hourly aggregation."""

from datetime import date, datetime, timedelta, timezone
import json

import pytest

from custom_components.lus.api import (
    LusAuthError,
    LusClient,
    Meter,
    Read,
    parse_meters,
    parse_reads,
    to_hourly,
)
from custom_components.lus.coordinator import _chunks, statistic_id

CDT = timezone(timedelta(hours=-5))
CST = timezone(timedelta(hours=-6))

METADATA = {
    "fuelTypeServicePoint": {
        "ELECTRICITY": [
            {
                "accountUuid": "elec-uuid",
                "fuelType": "ELECTRICITY",
                "preferredUtilityAccountId": "1000000001",
                "readResolution": "QUARTER_HOUR",
                "hasAmiUsage": True,
                "dateOfFirstAmiRead": "2023-09-30",
            }
        ],
        "WATER": [
            {
                "accountUuid": "water-uuid",
                "fuelType": "WATER",
                "preferredUtilityAccountId": "1000000002",
                "readResolution": "HOUR",
                "hasAmiUsage": True,
                "dateOfFirstAmiRead": None,
            }
        ],
        "GAS": [
            {
                "accountUuid": "gas-uuid",
                "preferredUtilityAccountId": "1000000003",
                "readResolution": "HOUR",
                "hasAmiUsage": False,
            }
        ],
    }
}


def quarter_reads(start: datetime, count: int, value: float = 0.25) -> list[Read]:
    step = timedelta(minutes=15)
    return [Read(start + i * step, start + (i + 1) * step, value) for i in range(count)]


def test_parse_meters_skips_meters_without_interval_data():
    meters = parse_meters(METADATA)
    assert [m.fuel_type for m in meters] == ["ELECTRICITY", "WATER"]
    elec, water = meters
    assert elec.aggregate_type == "quarter_hour" and elec.reads_per_hour == 4
    assert elec.first_read == date(2023, 9, 30)
    assert water.aggregate_type == "hour" and water.reads_per_hour == 1
    assert water.first_read is None


def test_parse_reads_skips_missing_values():
    body = {
        "reads": [
            {
                "startTime": "2026-10-26T00:00:00.000-05:00",
                "endTime": "2026-10-26T00:15:00.000-05:00",
                "consumption": {"value": 0.144, "type": "ACTUAL"},
            },
            {
                "startTime": "2026-10-26T00:15:00.000-05:00",
                "endTime": "2026-10-26T00:30:00.000-05:00",
                "consumption": None,
            },
        ]
    }
    reads = parse_reads(body)
    assert len(reads) == 1
    assert reads[0].start == datetime(2026, 10, 26, tzinfo=CDT)
    assert reads[0].value == pytest.approx(0.144)


def test_to_hourly_sums_quarter_hours_and_holds_back_partial_last_hour():
    start = datetime(2026, 10, 26, tzinfo=CDT)
    hours = to_hourly(quarter_reads(start, 4 * 24 + 2), reads_per_hour=4)
    assert len(hours) == 24
    assert all(h.value == pytest.approx(1.0) for h in hours)
    assert hours[-1].start == start + timedelta(hours=23)


def test_to_hourly_accepts_old_incomplete_hours():
    start = datetime(2026, 10, 20, tzinfo=CDT)
    reads = quarter_reads(start, 4 * 24 * 5)
    del reads[5]  # one missing read in the first hour-and-a-half, days ago
    hours = to_hourly(reads, reads_per_hour=4)
    assert len(hours) == 24 * 5
    assert hours[1].value == pytest.approx(0.75)


def hourly_reads(start: datetime, count: int) -> list[Read]:
    step = timedelta(hours=1)
    return [Read(start + i * step, start + (i + 1) * step, 1.0) for i in range(count)]


def test_to_hourly_waits_at_a_recent_hour_with_no_reads():
    reads = hourly_reads(datetime(2026, 10, 26, tzinfo=CDT), 24)
    del reads[20]  # 20:00 isn't in yet, though 21:00-23:00 are
    hours = to_hourly(reads, reads_per_hour=1)
    assert [h.start.hour for h in hours] == list(range(20))


def test_to_hourly_leaves_out_an_old_hour_with_no_reads():
    start = datetime(2026, 10, 20, tzinfo=CDT)
    reads = hourly_reads(start, 24 * 5)
    del reads[5]  # days ago
    hours = to_hourly(reads, reads_per_hour=1)
    assert len(hours) == 24 * 5 - 1
    assert hours[5].start == start + timedelta(hours=6)


def test_to_hourly_handles_dst_fall_back():
    # 2026-11-01 01:00 happens twice in Central time.
    first = quarter_reads(datetime(2026, 11, 1, 0, tzinfo=CDT), 8)
    second = quarter_reads(datetime(2026, 11, 1, 1, tzinfo=CST), 4)
    hours = to_hourly(first + second, reads_per_hour=4)
    assert [h.start.astimezone(timezone.utc).hour for h in hours] == [5, 6, 7]


def test_chunks_cover_range_inclusively():
    chunks = _chunks(date(2026, 1, 1), date(2026, 3, 5), 30)
    assert chunks[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert chunks[-1][1] == date(2026, 3, 5)
    for (_, end), (start, _) in zip(chunks, chunks[1:]):
        assert start == end + timedelta(days=1)


def test_statistic_id_is_valid():
    meter = Meter("u", "1000000001", "WASTE_WATER", "HOUR", None)
    assert statistic_id(meter) == "lus:1000000001_wastewater_consumption"


class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers requests from a queue of (method, path suffix, status, body)."""

    def __init__(self, responses: list[tuple[str, str, int, dict]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for i, (m, suffix, status, body) in enumerate(self._responses):
            if m == method and url.endswith(suffix):
                del self._responses[i]
                return FakeResponse(status, body)
        raise AssertionError(f"Unexpected {method} {url}")

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def sent(self, suffix: str) -> list[dict]:
        return [kw for _, url, kw in self.calls if url.endswith(suffix)]


LOGIN = "/identity-management-v1/cws/v1/lus/login"
PRIMARY = "/customer-preferences-v1/cws/v1/accounts/primary"
CURRENT = "/multi-account-v1/cws/lus/customers/current"
META = "/DataBrowser-v1/cws/metadata"
READS = "/utilityAccounts/elec-uuid/reads"


def login_responses(token: str = "tok-1") -> list:
    return [
        ("POST", LOGIN, 200, {"sessionToken": token}),
        ("GET", PRIMARY, 200, {"id": "1000000099"}),
        ("GET", CURRENT, 200, {"uuid": "cust-uuid"}),
    ]


async def test_login_and_reads_send_expected_headers():
    session = FakeSession(
        login_responses()
        + [("GET", META, 200, METADATA), ("GET", READS, 200, {"reads": []})]
    )
    client = LusClient(session, "user", "pw")
    await client.async_login()
    meters = await client.async_get_meters()
    await client.async_get_reads(meters[0], date(2026, 10, 26), date(2026, 10, 27))

    assert session.sent(LOGIN)[0]["data"] == {
        "username": "user",
        "password": "pw",
        "rememberMe": "true",
    }
    current = session.sent(CURRENT)[0]["headers"]
    assert json.loads(current["Opower-Selected-Entities"]) == [
        "urn:session:account:1000000099"
    ]
    reads = session.sent(READS)[0]
    assert reads["headers"]["Authorization"] == "Bearer tok-1"
    assert json.loads(reads["headers"]["Opower-Selected-Entities"]) == [
        "urn:opower:customer:uuid:cust-uuid",
        "urn:session:account:1000000099",
    ]
    assert reads["params"]["aggregateType"] == "quarter_hour"
    assert reads["params"]["startDate"] == "2026-10-26"
    assert reads["params"]["endDate"] == "2026-10-27"


async def test_expired_session_logs_in_again_once():
    session = FakeSession(
        login_responses("tok-1")
        + [("GET", META, 401, {"error": {"serviceErrorCode": "UNAUTHORIZED"}})]
        + login_responses("tok-2")
        + [("GET", META, 200, METADATA)]
    )
    client = LusClient(session, "user", "pw")
    await client.async_login()
    meters = await client.async_get_meters()
    assert len(meters) == 2
    assert session.sent(META)[-1]["headers"]["Authorization"] == "Bearer tok-2"


async def test_session_rejected_twice_raises_auth_error():
    session = FakeSession(
        login_responses("tok-1")
        + [("GET", META, 401, {})]
        + login_responses("tok-2")
        + [("GET", META, 401, {})]
    )
    client = LusClient(session, "user", "pw")
    await client.async_login()
    with pytest.raises(LusAuthError):
        await client.async_get_meters()


async def test_bad_password_raises_auth_error():
    session = FakeSession(
        [("POST", LOGIN, 401, {"error": {"serviceErrorCode": "INVALID_CREDENTIALS"}})]
    )
    with pytest.raises(LusAuthError):
        await LusClient(session, "user", "bad").async_login()
