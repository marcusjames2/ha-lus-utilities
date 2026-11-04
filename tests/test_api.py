"""Offline tests for the LUS client and hourly aggregation."""

from datetime import date, datetime, timedelta, timezone
import json

import pytest

from custom_components.lus.api import (
    LusAuthError,
    LusClient,
    LusError,
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


def test_parse_reads_accepts_an_explicit_empty_collection():
    assert parse_reads({"reads": []}) == []


@pytest.mark.parametrize(
    "empty_interval",
    [
        {},
        {"consumption": None},
        {"consumption": {}},
        {"consumption": {"value": None}},
    ],
)
def test_parse_reads_skips_legitimate_empty_intervals(empty_interval):
    assert parse_reads({"reads": [empty_interval]}) == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (None, "expected a JSON object"),
        ([], "expected a JSON object"),
        ({}, "missing 'reads'"),
        ({"reads": None}, "'reads' must be a list"),
        ({"reads": {}}, "'reads' must be a list"),
    ],
)
def test_parse_reads_rejects_malformed_payloads(body, message):
    with pytest.raises(LusError, match=message):
        parse_reads(body)


VALID_READ = {
    "startTime": "2026-10-26T00:00:00.000-05:00",
    "endTime": "2026-10-26T00:15:00.000-05:00",
    "consumption": {"value": 0.144},
}


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (None, r"read\[0\].*JSON object"),
        ([], r"read\[0\].*JSON object"),
        ({"consumption": "missing"}, r"read\[0\].*consumption.*JSON object or null"),
        ({"consumption": []}, r"read\[0\].*consumption.*JSON object or null"),
        (
            {**VALID_READ, "startTime": None},
            r"read\[0\].*startTime.*timezone-aware ISO 8601 string",
        ),
        (
            {key: value for key, value in VALID_READ.items() if key != "startTime"},
            r"read\[0\].*startTime.*timezone-aware ISO 8601 string",
        ),
        (
            {**VALID_READ, "startTime": "not-a-date"},
            r"read\[0\].*startTime.*timezone-aware ISO 8601 string",
        ),
        (
            {**VALID_READ, "startTime": "2026-10-26T00:00:00"},
            r"read\[0\].*startTime.*timezone-aware ISO 8601 string",
        ),
        (
            {**VALID_READ, "endTime": 123},
            r"read\[0\].*endTime.*timezone-aware ISO 8601 string",
        ),
        (
            {key: value for key, value in VALID_READ.items() if key != "endTime"},
            r"read\[0\].*endTime.*timezone-aware ISO 8601 string",
        ),
        (
            {**VALID_READ, "endTime": "2026-10-26"},
            r"read\[0\].*endTime.*timezone-aware ISO 8601 string",
        ),
        (
            {**VALID_READ, "consumption": {"value": "0.144"}},
            r"read\[0\].*consumption.value.*finite number",
        ),
        (
            {**VALID_READ, "consumption": {"value": True}},
            r"read\[0\].*consumption.value.*finite number",
        ),
        (
            {**VALID_READ, "consumption": {"value": float("nan")}},
            r"read\[0\].*consumption.value.*finite number",
        ),
        (
            {**VALID_READ, "consumption": {"value": float("inf")}},
            r"read\[0\].*consumption.value.*finite number",
        ),
        (
            {**VALID_READ, "consumption": {"value": 10**1000}},
            r"read\[0\].*consumption.value.*finite number",
        ),
    ],
)
def test_parse_reads_rejects_malformed_individual_reads(item, message):
    with pytest.raises(LusError, match=message):
        parse_reads({"reads": [item]})


def test_parse_reads_error_identifies_later_read_index():
    with pytest.raises(LusError, match=r"read\[1\].*consumption.value"):
        parse_reads({"reads": [VALID_READ, {**VALID_READ, "consumption": {"value": {}}}]})


def test_parse_reads_accepts_integer_and_float_consumption():
    second = {
        **VALID_READ,
        "startTime": "2026-10-26T00:15:00.000-05:00",
        "endTime": "2026-10-26T00:30:00.000-05:00",
        "consumption": {"value": 2},
    }
    reads = parse_reads({"reads": [VALID_READ, second]})
    assert [read.value for read in reads] == pytest.approx([0.144, 2.0])


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
    def __init__(self, status: int, body) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
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


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (ValueError("not json"), "not valid JSON"),
        ("not an object", "expected a JSON object"),
        ({}, "missing 'reads'"),
        ({"reads": "not a list"}, "'reads' must be a list"),
        ({"reads": [None]}, r"read\[0\].*JSON object"),
        (
            {"reads": [{"consumption": {"value": "1.0"}}]},
            r"read\[0\].*consumption.value.*finite number",
        ),
    ],
)
async def test_get_reads_rejects_malformed_success_responses(body, message):
    session = FakeSession(login_responses() + [("GET", READS, 200, body)])
    client = LusClient(session, "user", "pw")
    await client.async_login()

    meter = Meter("elec-uuid", "1000000001", "ELECTRICITY", "QUARTER_HOUR", None)
    with pytest.raises(LusError, match=message):
        await client.async_get_reads(meter, date(2026, 10, 26), date(2026, 10, 27))


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
