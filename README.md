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
- Wastewater is billed from the water meter, so its numbers usually match water.

## Estimated cost

The portal's interval reads carry no cost, so the integration estimates it
from your bill's rates and adds three more statistics:

| Statistic | What it includes |
|---|---|
| `lus:<id>_electricity_cost` | kWh × (energy + fuel adjustment) + electric customer charge |
| `lus:<id>_water_cost` | Tiered water + wastewater volume (with the summer discount) + water, state and wastewater fixed charges |
| `lus:<account>_total_cost` | Electricity + water costs + garbage, for hours both meters have reported |

- Monthly fixed charges are spread evenly over the hours of each billing cycle.
- Water tiers reset at the start of each billing cycle (midnight on the meter
  read day, the 23rd by default).
- In summer billing months, wastewater isn't charged on gallons above the
  tier-1 limit, matching the bill's "Summer Discount" line.
- Checked against the October 2026 bill: the model reproduces its $475.39 in
  current charges to within a cent when usage is spread across the cycle.

**Using it in the Energy dashboard:** on the LUS electricity entry choose
"Use an entity tracking the total costs" and pick `LUS electricity cost …`; do
the same for water with `LUS water cost …`. Don't add the wastewater meter as a
water source, or every gallon counts twice. To see the whole estimated bill,
add a Statistics graph card for `LUS total cost …` with the "change" stat and a
monthly period.

**Updating rates:** Settings → Devices & services → LUS → Configure. The
defaults come from the October 2026 bill. The fuel adjustment changes most
months; update it from each bill. New rates apply to hours imported after the
change; hours already stored keep the cost they were imported with. The
backfill uses the current rates for all of history, so older months are only
approximate.

**Not modeled:** taxes (currently $0 on the bill), late fees, payments and
credits, and any change in the tier-1 limit between cycles. The tier-1 limit
of 20,470 gallons is taken from the October bill; if LUS prorates it by cycle
length, months with a different number of days will be slightly off. Which
months get the summer wastewater discount is a guess (May–October) apart
from October, which the bill confirms; adjust it in the options if a bill shows
otherwise.

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
