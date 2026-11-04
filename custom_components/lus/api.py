"""Client for the LUS (Lafayette Utilities System) Opower DSS portal.

Kept free of Home Assistant imports so it can be run and tested on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from typing import Any

import aiohttp

BASE_URL = "https://dss-lus.opower.com/webcenter/edge/apis"
CLIENT_CODE = "lus"

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
        ) as resp:
            body = await _json(resp)
            if resp.status in (400, 401, 403):
                code = (body.get("error") or {}).get("serviceErrorCode")
                raise LusAuthError(f"Login rejected ({resp.status} {code})")
            if resp.status != 200:
                raise LusError(f"Login failed with HTTP {resp.status}")
        token = body.get("sessionToken")
        if not token:
            raise LusAuthError("Login response had no session token")
        self._token = token

        account = await self._get(
            "customer-preferences-v1/cws/v1/accounts/primary", entities=False
        )
        self._account_id = str(account["id"])
        customer = await self._get(
            f"multi-account-v1/cws/{CLIENT_CODE}/customers/current",
            params={"X-DS-4930-uom-enable": "true"},
            entities=False,
            account_only=True,
        )
        self._customer_uuid = customer["uuid"]

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
        )
        return parse_reads(body)

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        entities: bool = True,
        account_only: bool = False,
        retry: bool = True,
    ) -> dict[str, Any]:
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
            f"{BASE_URL}/{path}", params=params, headers=headers
        ) as resp:
            body = await _json(resp)
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
            )
        if status == 401:
            raise LusAuthError("Session rejected after re-login")
        if status != 200:
            detail = (body.get("error") or body).get("details")
            raise LusError(f"GET {path} returned HTTP {status}: {detail}")
        return body


async def _json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def parse_meters(meta: dict[str, Any]) -> list[Meter]:
    """Build meters from a DataBrowser metadata response."""
    meters = []
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
    return meters


def parse_reads(body: dict[str, Any]) -> list[Read]:
    """Build reads from a DataBrowser reads response, skipping empty intervals."""
    reads = []
    for item in body.get("reads") or []:
        value = (item.get("consumption") or {}).get("value")
        if value is None:
            continue
        reads.append(
            Read(
                start=datetime.fromisoformat(item["startTime"]),
                end=datetime.fromisoformat(item["endTime"]),
                value=float(value),
            )
        )
    return reads


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
