# LUS (Lafayette Utilities System) for Home Assistant

Imports smart-meter usage from the LUS customer portal (`dss-lus.opower.com`)
into Home Assistant long-term statistics, for use in the Energy dashboard.

The built-in Opower integration doesn't list LUS: LUS runs Opower's DSS portal,
which has its own login and API path. This integration talks to it directly.

## What it imports

One statistic per meter with interval data on the account's primary LUS account:

| Meter | Portal resolution | Statistic | Unit |
|---|---|---|---|
| Electric | 15 min (summed to hours) | `lus:<id>_electricity_consumption` | kWh |
| Water | Hourly | `lus:<id>_water_consumption` | gal |
| Wastewater | Hourly | `lus:<id>_wastewater_consumption` | gal |

- The first run backfills from the meter's first smart-meter read (LUS data
  starts around September 2023), 30 days per request.
- After that it checks every 6 hours. The portal lags about 1 day for electric
  and 1–2 days for water, so the newest data is always a day or two old.
- An hour is imported only once all of its reads are in, so partial hours are
  never stored short. Hours with missing reads older than 48 hours are
  imported as they are.
- Interval reads carry no cost. Set a price per kWh / gallon in the Energy
  dashboard if you want cost tracking.
- Wastewater is billed from the water meter, so its numbers usually match water.

## Install

1. Copy `custom_components/lus` into your Home Assistant `config/custom_components/`
   (or add this repository to HACS as a custom repository).
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **LUS (Lafayette Utilities System)**.
4. Enter your portal username and password.
5. Settings → Dashboards → Energy: add `LUS electricity …` under Electricity grid
   consumption and `LUS water …` under Water consumption.

Requires Home Assistant 2026.1 or newer.

## Check your login outside Home Assistant

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp
.venv/bin/python scripts/probe.py 3
```

It prompts for the username and password (or reads `LUS_USERNAME` /
`LUS_PASSWORD`), logs in, and prints a summary of the last 3 days per meter.

## Development

```bash
.venv/bin/pip install homeassistant pytest pytest-asyncio
.venv/bin/python -m pytest
```

## How the portal API works

All calls go to `https://dss-lus.opower.com/webcenter/edge/apis/`.

1. `POST identity-management-v1/cws/v1/lus/login`, form-encoded `username`,
   `password`, `rememberMe` → `{"sessionToken": ...}`.
2. Every later call sends `Authorization: Bearer <sessionToken>`.
3. `GET customer-preferences-v1/cws/v1/accounts/primary` → account `id`.
4. `GET multi-account-v1/cws/lus/customers/current` with header
   `Opower-Selected-Entities: ["urn:session:account:<id>"]` → customer `uuid`.
5. DataBrowser calls send
   `Opower-Selected-Entities: ["urn:opower:customer:uuid:<uuid>", "urn:session:account:<id>"]`:
   - `GET DataBrowser-v1/cws/metadata?customerUuid=<uuid>&includeCommercialAndIndustrial=true&preferredUtilityAccountIdType=UTILITY_ACCOUNT_ID_1`
     lists meters (`accountUuid`, `readResolution`, `dateOfFirstAmiRead`).
   - `GET DataBrowser-v1/cws/utilities/lus/utilityAccounts/<accountUuid>/reads?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&aggregateType=quarter_hour|hour|day|bill`
     returns reads; both dates are inclusive.

A 401 means the session expired; the client logs in again once and retries.
