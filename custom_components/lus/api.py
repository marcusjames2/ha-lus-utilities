"""Client for the LUS (Lafayette Utilities System) Opower DSS portal.

Kept free of Home Assistant imports so it can be run and tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
from typing import Any

import aiohttp

BASE_URL = "https://dss-lus.opower.com/webcenter/edge/apis"
CLIENT_CODE = "lus"

# Longest a single portal request may take. aiohttp's own default is five
# minutes, which would let one stalled call hold up a whole update.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Fuel types reported by DataBrowser metadata.
FUEL_ELECTRICITY = "ELECTRICITY"
FUEL_WATER = "WATER"
FUEL_WASTE_WATER = "WASTE_WATER"

# readResolution -> (aggregateType for the reads call, reads per hour)
_RESOLUTIONS = {
    "QUARTER_HOUR": ("quarter_hour", 4),
    "HALF_HOUR": ("half_hour", 2),
    "HOUR": ("hour", 1),
}


class LusError(Exception):
    """Unexpected response from the portal."""


class LusAuthError(LusError):
    """Login failed or the session could not be re-established."""


@dataclass(frozen=True)
class Meter:
    """One metered service on the account."""

    uuid: str
    utility_account_id: str
    fuel_type: str
    read_resolution: str
    first_read: date | None

    @property
    def aggregate_type(self) -> str:
        return _RESOLUTIONS.get(self.read_resolution, ("hour", 1))[0]

    @property
    def reads_per_hour(self) -> int:
        return _RESOLUTIONS.get(self.read_resolution, ("hour", 1))[1]


@dataclass(frozen=True)
class Read:
    """One interval read."""

    start: datetime
    end: datetime
    value: float


@dataclass(frozen=True)
class HourlyUsage:
    """Usage summed to a full clock hour."""

    start: datetime
    value: float


class LusClient:
    """Logs in to the portal and fetches meter reads."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = None
        self._account_id: str | None = None
        self._customer_uuid: str | None = None

    async def async_login(self) -> None:
        """Log in and resolve the account and customer the token belongs to."""
        async with self._session.post(
            f"{BASE_URL}/identity-management-v1/cws/v1/{CLIENT_CODE}/login",
            data={
                "username": self._username,
                "password": self._password,
                "rememberMe": "true",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            body = await _json(resp)
            status = resp.status
        # The portal answers a rejected login with a JSON error. A 400 or 403
        # without one is a block or an outage page, not a wrong password, and
        # must not send the user off to re-enter credentials that are fine.
        if status == 401 or (status in (400, 403) and body is not None):
            code = _error(body).get("serviceErrorCode")
            raise LusAuthError(f"Login rejected ({status} {code})")
        if status != 200:
            raise LusError(f"Login failed with HTTP {status}")
        if body is None:
            raise LusError("Login returned something other than JSON")
        token = body.get("sessionToken")
        if not token:
            raise LusAuthError("Login response had no session token")
        self._token = token

        # These two must not log in again on a 401: that is this method, and
        # the two would call each other, password and all, without end.
        account = await self._get(
            "customer-preferences-v1/cws/v1/accounts/primary",
            entities=False,
            retry=False,
        )
        self._account_id = str(_field(account, "id"))
        customer = await self._get(
            f"multi-account-v1/cws/{CLIENT_CODE}/customers/current",
            params={"X-DS-4930-uom-enable": "true"},
            entities=False,
            account_only=True,
            retry=False,
        )
        self._customer_uuid = _field(customer, "uuid")

    @property
    def account_id(self) -> str | None:
        """Primary LUS account number, known after login."""
        return self._account_id

    async def async_get_meters(self) -> list[Meter]:
        """Return the meters on the primary account that have interval data."""
        meta = await self._get(
            "DataBrowser-v1/cws/metadata",
            params={
                "preferredUtilityAccountIdType": "UTILITY_ACCOUNT_ID_1",
                "includeCommercialAndIndustrial": "true",
                "customerUuid": self._customer_uuid,
            },
        )
        return parse_meters(meta)

    async def async_get_reads(
        self, meter: Meter, start: date, end: date
    ) -> list[Read]:
        """Return interval reads from start through end, both dates inclusive."""
        body = await self._get(
            f"DataBrowser-v1/cws/utilities/{CLIENT_CODE}/utilityAccounts/{meter.uuid}/reads",
            params={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "aggregateType": meter.aggregate_type,
                "includeEnhancedBilling": "false",
                "includeMultiRegisterData": "false",
            },
            strict_json=True,
        )
        return parse_reads(body)

    async def async_get_bill_periods(self, meter: Meter) -> list[tuple[datetime, datetime]]:
        """Return the meter's billing periods, oldest first, as (start, end).

        Without dates, the bill aggregation returns every bill on the account.
        """
        body = await self._get(
            f"DataBrowser-v1/cws/utilities/{CLIENT_CODE}/utilityAccounts/{meter.uuid}/reads",
            params={
                "aggregateType": "bill",
                "includeEnhancedBilling": "false",
                "includeMultiRegisterData": "false",
            },
        )
        return parse_bill_periods(body)

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        entities: bool = True,
        account_only: bool = False,
        retry: bool = True,
        strict_json: bool = False,
    ) -> Any:
        if self._token is None:
            raise LusAuthError("Not logged in")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Requested-With": "XMLHttpRequest",
        }
        selected = []
        if entities and self._customer_uuid:
            selected.append(f"urn:opower:customer:uuid:{self._customer_uuid}")
        if (entities or account_only) and self._account_id:
            selected.append(f"urn:session:account:{self._account_id}")
        if selected:
            headers["Opower-Selected-Entities"] = json.dumps(selected)

        async with self._session.get(
            f"{BASE_URL}/{path}",
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            body = await _json(resp, strict=strict_json and resp.status == 200)
            status = resp.status
        if status == 401 and retry:
            # Sessions expire; log in again once and repeat the call.
            await self.async_login()
            return await self._get(
                path,
                params,
                entities=entities,
                account_only=account_only,
                retry=False,
                strict_json=strict_json,
            )
        if status == 401:
            raise LusAuthError("Session rejected right after logging in")
        # Meter ids are part of the reads path; keep them out of error text,
        # which ends up in the log and in bug reports.
        endpoint = path.split("/utilityAccounts/")[0]
        if status != 200:
            detail = str(_error(body).get("details") or "")[:200]
            raise LusError(f"GET {endpoint} returned HTTP {status}: {detail}")
        if body is None:
            # Taking a maintenance page for an empty answer would leave a hole
            # in the history that later imports never go back to fill.
            raise LusError(f"GET {endpoint} returned something other than JSON")
        return body


async def _json(resp: aiohttp.ClientResponse, *, strict: bool = False) -> Any:
    """The response's JSON: any JSON value when `strict`, which raises if the
    body isn't JSON at all; otherwise the JSON object, or None if it isn't one."""
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        if strict:
            raise LusError("Malformed reads response: body was not valid JSON") from None
        return None
    return body if strict or isinstance(body, dict) else None


def _error(body: Any) -> dict[str, Any]:
    """The error object of a response, whatever shape the portal gave it."""
    body = body if isinstance(body, dict) else {}
    error = body.get("error")
    if isinstance(error, dict):
        return error
    return {"details": error if error is not None else body.get("details")}


def _field(body: dict[str, Any], key: str) -> Any:
    """A field the portal always sends; without it the response is unusable."""
    value = body.get(key)
    if value is None:
        raise LusError(f"Portal response had no {key}")
    return value


def parse_meters(meta: dict[str, Any]) -> list[Meter]:
    """Build meters from a DataBrowser metadata response."""
    meters = []
    try:
        for fuel_type, points in (meta.get("fuelTypeServicePoint") or {}).items():
            for point in points:
                if not point.get("hasAmiUsage"):
                    continue
                first = point.get("dateOfFirstAmiRead")
                meters.append(
                    Meter(
                        uuid=point["accountUuid"],
                        utility_account_id=str(point["preferredUtilityAccountId"]),
                        fuel_type=fuel_type,
                        read_resolution=point.get("readResolution") or "HOUR",
                        first_read=date.fromisoformat(first) if first else None,
                    )
                )
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        raise LusError("Portal meter list was not in the expected form") from err
    return meters


def parse_reads(body: Any) -> list[Read]:
    """Build reads from a DataBrowser reads response, skipping empty intervals."""
    if not isinstance(body, dict):
        raise LusError("Malformed reads response: expected a JSON object")
    if "reads" not in body:
        raise LusError("Malformed reads response: missing 'reads'")
    items = body["reads"]
    if not isinstance(items, list):
        raise LusError("Malformed reads response: 'reads' must be a list")

    reads = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise LusError(
                f"Malformed reads response: read[{index}] must be a JSON object"
            )

        consumption = item.get("consumption")
        if consumption is None:
            continue
        if not isinstance(consumption, Mapping):
            raise LusError(
                "Malformed reads response: "
                f"read[{index}].consumption must be a JSON object or null"
            )
        value = consumption.get("value")
        if value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError):
            numeric_value = math.nan
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(numeric_value)
        ):
            raise LusError(
                "Malformed reads response: "
                f"read[{index}].consumption.value must be a finite number"
            )

        reads.append(
            Read(
                start=_read_datetime(item, "startTime", index),
                end=_read_datetime(item, "endTime", index),
                value=numeric_value,
            )
        )
    return reads


def _read_datetime(item: Mapping[str, Any], field: str, index: int) -> datetime:
    """Return one required timezone-aware read timestamp."""
    value = item.get(field)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed
    raise LusError(
        "Malformed reads response: "
        f"read[{index}].{field} must be a timezone-aware ISO 8601 string"
    )


def parse_bill_periods(body: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    """Build billing periods from a bill-aggregated reads response."""
    reads = body.get("reads")
    if reads is None:
        return []
    if not isinstance(reads, Sequence) or isinstance(
        reads, (str, bytes, bytearray)
    ):
        raise LusError("billing response 'reads' must be a sequence")
    periods = []
    for index, item in enumerate(reads):
        if not isinstance(item, Mapping):
            raise LusError(f"billing period {index} must be a mapping")
        start_text = item.get("startTime")
        end_text = item.get("endTime")
        if start_text is None and end_text is None:
            # The undated aggregation can include non-period account-level items.
            continue
        if start_text is None or end_text is None:
            raise LusError(f"billing period {index} has only one endpoint")
        if (
            not isinstance(start_text, str)
            or not start_text
            or not isinstance(end_text, str)
            or not end_text
        ):
            raise LusError(f"billing period {index} has an invalid endpoint")
        try:
            periods.append(
                (datetime.fromisoformat(start_text), datetime.fromisoformat(end_text))
            )
        except (TypeError, ValueError) as err:
            raise LusError(f"billing period {index} has an invalid date") from err
    return validate_bill_periods(periods)


def validate_bill_periods(
    periods: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Return chronological, continuous, positive, timezone-aware bill periods."""
    checked = []
    for index, period in enumerate(periods):
        try:
            start, end = period
        except (TypeError, ValueError) as err:
            raise LusError(f"billing period {index} is not a (start, end) pair") from err
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise LusError(f"billing period {index} endpoints must be datetimes")
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
        ):
            raise LusError(f"billing period {index} has a naive datetime endpoint")
        if end <= start:
            raise LusError(f"billing period {index} has a non-positive duration")
        checked.append((start, end))

    checked.sort(key=lambda period: period[0])
    for index, (previous, current) in enumerate(zip(checked, checked[1:]), start=1):
        if current[0] < previous[1]:
            raise LusError(f"billing period {index} overlaps the previous billing period")
        if current[0] > previous[1]:
            raise LusError(
                f"billing period {index} is discontinuous from the previous billing period"
            )
    return checked


def to_hourly(
    reads: Iterable[Read],
    reads_per_hour: int,
    settle: timedelta = timedelta(hours=48),
) -> list[HourlyUsage]:
    """Sum reads into clock hours.

    Within `settle` of the newest read, stops at the first incomplete hour, or
    at the first hour with no reads at all, so an hour still being reported is
    picked up whole on a later update instead of being imported short or
    skipped for good. Older incomplete hours are taken as they are and older
    hours with no reads are left out, so a permanent gap in the meter data
    can't stall imports.
    """
    reads = list(reads)
    if not reads:
        return []
    cutoff = max(r.end for r in reads) - settle

    buckets: dict[datetime, list[Read]] = {}
    for read in reads:
        hour = read.start.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(read)

    hours: list[HourlyUsage] = []
    for hour in sorted(buckets):
        group = buckets[hour]
        if hour >= cutoff and len(group) < reads_per_hour:
            break
        if hours and next_hour(hours[-1].start) >= cutoff and hour > next_hour(hours[-1].start):
            break
        hours.append(HourlyUsage(start=hour, value=sum(r.value for r in group)))
    return hours


def next_hour(hour: datetime) -> datetime:
    """The start of the hour after `hour`, in real time across DST changes."""
    return hour.astimezone(timezone.utc) + timedelta(hours=1)
