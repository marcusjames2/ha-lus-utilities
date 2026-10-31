# LUS (Lafayette Utilities System) for Home Assistant

Imports smart-meter usage from the LUS customer portal (`dss-lus.opower.com`)
into Home Assistant long-term statistics, for use in the Energy dashboard.

The built-in Opower integration doesn't list LUS: LUS runs Opower's DSS portal,
which has its own login and API path. This integration talks to it directly.

> **Disclaimer:** This is an unofficial, independent project. It is not
> affiliated with, endorsed by, or supported by Lafayette Utilities System
> (LUS), the Lafayette Utilities Authority, or Opower/Oracle. The LUS name and
> logo belong to their owner and are used only to identify the utility. Cost
> figures are estimates; your LUS bill is the authoritative amount. The
> integration reads the same customer portal you sign in to, so changes to that
> portal may break it.

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
- Water and wastewater follow LUS rate schedules W1 and S1. In April–November
  bills, water above the tier-1 limit is charged at the tier-2 rate, and
  wastewater is charged on the tier-1 limit or actual use, whichever is less
  (the bill's "Summer Discount" line), but never on less than 75% of actual
  use. December–March bills charge every gallon at the tier-1 rate and all of
  it for wastewater.
- Water tiers reset at the start of each billing cycle (midnight on the meter
  read day, the 23rd by default).
- Checked against the October 2026 bill: the model reproduces its $475.39 in
  current charges to within a cent when usage is spread across the cycle.

**Using it in the Energy dashboard:** on the LUS electricity entry choose
"Use an entity tracking the total costs" and pick `LUS electricity cost …`; do
the same for water with `LUS water cost …`. Don't add the wastewater meter as a
water source, or every gallon counts twice. To see the whole estimated bill,
add a Statistics graph card for `LUS total cost …` with the "change" stat and a
monthly period.

**Bill sensors:** an **LUS** device with three sensors, which the Overview
dashboard shows automatically (or add them to any dashboard):

| Sensor | Value |
|---|---|
| Estimated bill this period | Estimated charges since the last meter read, through the newest data |
| Projected bill this period | That amount scaled to the whole billing period at the same hourly rate |
| Estimated last bill | The most recent closed billing period; compare it with your bill |

Each has `period_start`, `period_end`, `data_through` and `complete`
attributes. They include every charge the cost model covers, garbage included,
and update with the statistics every 6 hours, so they trail by a day or two.

**Updating rates:** Settings → Devices & services → LUS → Configure.
1. Pick the date the rates take effect. It defaults to the start of the most
   recently closed billing cycle, which is the period on the bill you just
   received.
2. Enter the bill's rates. The form starts with the rates already in effect on
   that date.

Each set of rates applies from its date until the next later set, so you can
also go back and correct an older period without touching later ones. On the
next update, stored costs from that date on are recalculated and overwritten;
usage isn't touched. Hours before the earliest dated set use the original,
undated rates (the October 2026 bill unless you changed them), so backfilled
history before your first bill entry is only approximate.

**Upgrading from 0.4.0:** sets of rates still holding 0.4.0's guessed summer
months (May–October) move to April–November, and every stored cost is
recalculated on the first update under the corrected water and wastewater
rules. Months you changed yourself are kept.

**Not modeled:** taxes (currently $0 on the bill), late fees, payments and
credits. The tier-1 limit is the household's average water use over the
previous December–March bills, which LUS recalculates each year. The default
of 20,470 gallons comes from one household's October 2026 bill, so enter the
limit from your own bill, and add a new set of rates when it changes.

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
