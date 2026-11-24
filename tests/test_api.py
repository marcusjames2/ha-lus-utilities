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


async def test_bill_periods_come_from_the_bill_aggregation_without_dates():
    body = {
        "reads": [
            {
                "startTime": "2026-08-21T00:00:00.000-05:00",
                "endTime": "2026-09-23T00:00:00.000-05:00",
                "consumption": {"value": 3100.0, "type": "ACTUAL"},
            },
            {
                "startTime": "2026-07-22T00:00:00.000-05:00",
                "endTime": "2026-08-21T00:00:00.000-05:00",
                "consumption": {"value": 2900.0, "type": "ESTIMATED"},
            },
            {"consumption": {"value": 1.0}},  # no dates: skipped
        ]
    }
    session = FakeSession(
        login_responses() + [("GET", META, 200, METADATA), ("GET", READS, 200, body)]
    )
    client = LusClient(session, "user", "pw")
    await client.async_login()
    meters = await client.async_get_meters()

    periods = await client.async_get_bill_periods(meters[0])
    assert periods == [
        (datetime(2026, 7, 22, tzinfo=CDT), datetime(2026, 8, 21, tzinfo=CDT)),
        (datetime(2026, 8, 21, tzinfo=CDT), datetime(2026, 9, 23, tzinfo=CDT)),
    ]
    (sent,) = session.sent(READS)
    assert sent["params"]["aggregateType"] == "bill"
    assert "startDate" not in sent["params"] and "endDate" not in sent["params"]


@pytest.mark.parametrize(
    "item",
    [
        {"startTime": "2026-08-21T00:00:00-05:00"},
        {"endTime": "2026-09-23T00:00:00-05:00"},
        {"startTime": "2026-08-21T00:00:00-05:00", "endTime": None},
        {"startTime": None, "endTime": "2026-09-23T00:00:00-05:00"},
        {"startTime": "not-a-date", "endTime": "2026-09-23T00:00:00-05:00"},
        {"startTime": "2026-08-21T00:00:00", "endTime": "2026-09-23T00:00:00"},
    ],
)
def test_bill_periods_reject_partial_malformed_or_naive_dates(item):
    from custom_components.lus.api import LusError, parse_bill_periods

    with pytest.raises(LusError, match="billing period"):
        parse_bill_periods({"reads": [item]})


@pytest.mark.parametrize("item", [None, "dated read", 0, []])
def test_bill_periods_reject_non_mapping_read_items(item):
    from custom_components.lus.api import LusError, parse_bill_periods

    with pytest.raises(LusError, match=r"billing period 0 must be a mapping"):
        parse_bill_periods({"reads": [item]})


@pytest.mark.parametrize(
    "item",
    [
        {"startTime": "", "endTime": ""},
        {"startTime": 0, "endTime": 0},
        {"startTime": False, "endTime": False},
        {"startTime": 123, "endTime": "2026-09-23T00:00:00-05:00"},
        {"startTime": "2026-08-21T00:00:00-05:00", "endTime": []},
    ],
)
def test_bill_periods_reject_present_but_invalid_endpoints(item):
    from custom_components.lus.api import LusError, parse_bill_periods

    with pytest.raises(LusError, match=r"billing period 0 has an invalid endpoint"):
        parse_bill_periods({"reads": [item]})


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"startTime": None},
        {"endTime": None},
        {"startTime": None, "endTime": None},
    ],
)
def test_bill_periods_skip_records_with_no_dated_endpoints(item):
    from custom_components.lus.api import parse_bill_periods

    assert parse_bill_periods({"reads": [item]}) == []


@pytest.mark.parametrize("body", [{}, {"reads": None}])
def test_bill_periods_allow_missing_reads(body):
    from custom_components.lus.api import parse_bill_periods

    assert parse_bill_periods(body) == []


@pytest.mark.parametrize(
    "reads",
    [False, 0, "", "not a list", b"reads", {}, {"startTime": "ignored"}, object()],
)
def test_bill_periods_reject_invalid_reads_container(reads):
    from custom_components.lus.api import LusError, parse_bill_periods

    with pytest.raises(LusError, match=r"billing response 'reads' must be a sequence"):
        parse_bill_periods({"reads": reads})


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


# --- Failure handling --------------------------------------------------------


async def test_rejected_token_right_after_login_does_not_log_in_again():
    # The portal issues a token, then refuses it. Logging in again from inside
    # the login would repeat without end, posting the password each time.
    session = FakeSession(
        [("POST", LOGIN, 200, {"sessionToken": "tok-1"}), ("GET", PRIMARY, 401, {})]
    )
    with pytest.raises(LusAuthError):
        await LusClient(session, "user", "pw").async_login()
    assert len(session.sent(LOGIN)) == 1


async def test_blocked_login_is_not_reported_as_a_bad_password():
    # A 403 with no JSON error is a firewall or outage page.
    session = FakeSession([("POST", LOGIN, 403, "<html>Access denied</html>")])
    with pytest.raises(LusError) as err:
        await LusClient(session, "user", "pw").async_login()
    assert not isinstance(err.value, LusAuthError)

    session = FakeSession([("POST", LOGIN, 200, "<html>Maintenance</html>")])
    with pytest.raises(LusError) as err:
        await LusClient(session, "user", "pw").async_login()
    assert not isinstance(err.value, LusAuthError)


async def test_rejected_login_with_any_error_shape_is_an_auth_error():
    for status, body in (
        (403, {"error": {"serviceErrorCode": "ACCOUNT_LOCKED"}}),
        (401, {"error": "Unauthorized"}),
        (401, "Unauthorized"),
    ):
        session = FakeSession([("POST", LOGIN, status, body)])
        with pytest.raises(LusAuthError):
            await LusClient(session, "user", "pw").async_login()


async def test_login_response_without_an_account_is_an_error():
    session = FakeSession(
        [("POST", LOGIN, 200, {"sessionToken": "tok-1"}), ("GET", PRIMARY, 200, {})]
    )
    with pytest.raises(LusError, match="no id"):
        await LusClient(session, "user", "pw").async_login()


async def logged_in(extra: list) -> tuple[LusClient, FakeSession, list]:
    session = FakeSession(login_responses() + [("GET", META, 200, METADATA), *extra])
    client = LusClient(session, "user", "pw")
    await client.async_login()
    return client, session, await client.async_get_meters()


async def test_page_that_is_not_json_is_an_error_not_an_empty_day():
    client, _, meters = await logged_in([("GET", READS, 200, "<html>Maintenance</html>")])
    with pytest.raises(LusError, match="Malformed reads response"):
        await client.async_get_reads(meters[0], date(2026, 10, 26), date(2026, 10, 27))


async def test_errors_leave_out_the_meter_id_and_survive_any_error_shape():
    for body in ({"error": "Internal Server Error"}, {"error": {"details": "boom"}}, None):
        client, _, meters = await logged_in([("GET", READS, 500, body)])
        with pytest.raises(LusError) as err:
            await client.async_get_reads(meters[0], date(2026, 10, 26), date(2026, 10, 27))
        assert "HTTP 500" in str(err.value)
        assert "elec-uuid" not in str(err.value)


async def test_every_request_has_a_timeout():
    _, session, _ = await logged_in([])
    assert all(kwargs["timeout"].total == 60 for _, _, kwargs in session.calls)


def test_malformed_portal_data_is_a_portal_error():
    with pytest.raises(LusError):
        parse_meters({"fuelTypeServicePoint": {"ELECTRICITY": [{"hasAmiUsage": True}]}})
    with pytest.raises(LusError):
        parse_reads({"reads": [{"consumption": {"value": 1.0}, "startTime": "yesterday"}]})


def hourly_reads(start: datetime, count: int) -> list[Read]:
    step = timedelta(hours=1)
    return [Read(start + i * step, start + (i + 1) * step, 10.0) for i in range(count)]


def test_to_hourly_waits_for_an_hour_with_no_reads_at_all():
    # The water meter's 10:00 read hasn't reached the portal yet. Importing
    # the hours after it would leave 10:00 empty for good.
    start = datetime(2026, 10, 26, tzinfo=CDT)
    reads = hourly_reads(start, 24)
    del reads[10]
    hours = to_hourly(reads, reads_per_hour=1)
    assert [h.start for h in hours] == [start + timedelta(hours=i) for i in range(10)]

    quarter = [r for r in quarter_reads(start, 4 * 24) if r.start.hour != 10]
    assert len(to_hourly(quarter, reads_per_hour=4)) == 10


def test_to_hourly_steps_over_an_old_hour_with_no_reads():
    start = datetime(2026, 10, 20, tzinfo=CDT)
    reads = hourly_reads(start, 24 * 5)
    del reads[10]
    hours = to_hourly(reads, reads_per_hour=1)
    assert len(hours) == 24 * 5 - 1
    assert start + timedelta(hours=10) not in [h.start for h in hours]


async def test_rejected_token_on_the_second_lookup_does_not_log_in_again():
    session = FakeSession(
        [
            ("POST", LOGIN, 200, {"sessionToken": "tok-1"}),
            ("GET", PRIMARY, 200, {"id": "1000000099"}),
            ("GET", CURRENT, 401, {}),
        ]
    )
    with pytest.raises(LusAuthError):
        await LusClient(session, "user", "pw").async_login()
    assert len(session.sent(LOGIN)) == 1
