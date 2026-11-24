# The math behind the spending insights

Every figure the insight sensors report comes from the formulas below. Each has
its variables defined and a worked example. The examples use the rates on the
October 2026 bill, which are the integration's defaults, and the same numbers
as `tests/test_analytics.py`, so every example here is also a passing test.

| Symbol | Meaning | Default |
|---|---|---|
| `r_e` | Electric energy charge, $/kWh | 0.05224 |
| `r_f` | Electric fuel adjustment, $/kWh | 0.04400 |
| `C_e` | Electric customer charge, $/cycle | 13.00 |
| `r_1`, `r_2` | Water tier 1 and tier 2 rates, $/gal | 0.00209, 0.00331 |
| `T` | Water tier 1 limit, gal/cycle: your average over the previous December to March bills | 20,470 on that bill |
| `C_w` | Water customer charge, $/cycle | 6.99 |
| `S` | State safe drinking water fee, $/cycle | 1.00 |
| `r_s` | Wastewater volume charge, $/gal | 0.00747 |
| `C_s` | Wastewater customer charge, $/cycle | 11.29 |
| `G` | Garbage and environmental services, $/cycle | 38.04 |
| `H` | Real hours in a billing cycle | 720 for a 30-day cycle |

Taxes are $0 on current LUS bills and are not modelled. Fees are: the fuel
adjustment, the three customer charges, the state fee and garbage.

## 1. The bill for one billing cycle

For a cycle with `E` kWh and `W` gallons:

```
In an April to November bill (LUS schedules W1 and S1):
  tier1 = min(W, T)                    gallons billed at tier 1
  tier2 = W - tier1                    gallons billed at tier 2
  sewer = max(min(W, T), 0.75 × W)     wastewater gallons: capped at the limit,
                                       but never under 75% of use
In a December to March bill:
  tier1 = W        tier2 = 0        sewer = W

electricity = E × (r_e + r_f) + C_e
water       = tier1 × r_1 + tier2 × r_2 + C_w + S
wastewater  = sewer × r_s + C_s
total       = electricity + water + wastewater + G
```

`T` is whatever limit you entered with your rates. Left at 0, it is worked out
from the water meter: the mean gallons per bill over the previous December to
March bills. With no limit and no winter data, a summer bill has no tiers.

**Example: the October 2026 bill**, `E` = 2,147 kWh and `W` = 21,300 gal:

```
tier1 = min(21,300, 20,470) = 20,470      tier2 = 830
sewer = max(20,470, 0.75 × 21,300 = 15,975) = 20,470

electricity = 2,147 × 0.05224 + 2,147 × 0.04400 + 13.00
            = 112.16 + 94.47 + 13.00                      = $219.63
water       = 20,470 × 0.00209 + 830 × 0.00331 + 6.99 + 1.00
            = 42.78 + 2.75 + 7.99                         = $53.52
wastewater  = 20,470 × 0.00747 + 11.29 = 152.91 + 11.29   = $164.20
garbage                                                   = $38.04
total                                                     = $475.39
```

Of that, **fixed** charges are `C_e + C_w + S + C_s + G` = $70.32 and
**variable** charges are the remaining $405.07.

## 2. Cost hour by hour, and by calendar month or year

A bill covers a billing cycle, not a calendar month. Cycles follow the dates on
your own LUS bills (or a fixed day of the month if you set one). To
add costs up over any span, each hour `h` is given its own cost:

```
electricity(h) = kWh(h) × (r_e + r_f) + C_e / H
water(h)       = tier1(h) × r_1 + tier2(h) × r_2 + (C_w + S) / H
wastewater(h)  = sewer(h) × r_s + C_s / H
garbage(h)     = G / H
```

`tier1(h)` is the part of the hour's gallons that still fits under `T`, counting
gallons from the start of the cycle, and `sewer(h)` is the amount by which the
hour raises the cycle's wastewater gallons. `H` is the cycle's real length, so a fixed
charge spread over all of a cycle's hours adds back up to exactly the charge.
An hour the meter skipped still carries its share of the fixed charges, with no
usage.
A cycle that contains the November clock change has one hour more than its days
× 24 and one that contains the March change has one less: 23 October to 23
November 2026 is 31 × 24 + 1 = **745** hours.

A month's or a year's cost by utility is the sum of these over its hours:

```
cost(utility, period) = Σ utility(h)   for every hour h that starts in the period
```

**Example: September 2026** at a steady 2 kWh and 25 gal every hour. The month
has 720 hours: 22 days in the 23 Aug to 23 Sep cycle (31 days, `H` = 744) and 8
days in the 23 Sep to 23 Oct cycle (30 days, `H` = 720).

```
kWh              = 2 × 720 = 1,440
usage charges    = 1,440 × 0.09624                       = $138.59
share of cycles  = 22/31 + 8/30                          = 0.97634
customer charge  = 13.00 × 0.97634                       = $12.69
electricity                                              = $151.28
garbage          = 38.04 × 0.97634                       = $37.14
```

A calendar month therefore carries slightly more or less than one set of fixed
charges, depending on how its days fall across cycles.

## 3. Effective rates: what a kWh or a gallon really costs

For a period with electricity cost `X_e`, water cost `X_w`, wastewater cost
`X_s`, `E` kWh and `W` gallons:

```
all-in electricity rate    = X_e / E
usage-only electricity     = (X_e - fixed electricity charges) / E  = r_e + r_f
fixed share                = fixed electricity charges / X_e
all-in water rate          = X_w / W
all-in water + wastewater  = (X_w + X_s) / W
usage-only water           = (water volume + wastewater volume charges) / W
```

The all-in rate is what you paid per unit. The usage-only rate is what one more
unit costs, which is the number to use when judging a saving.

**Example: the October 2026 bill** from section 1:

```
all-in electricity    = 219.63 / 2,147           = $0.10229 per kWh
usage-only            = (112.16 + 94.47) / 2,147 = $0.09624 per kWh
fixed share           = 13.00 / 219.63           = 5.9%
all-in water          = 53.52 / 21,300           = $0.002513 per gal
water + wastewater    = (53.52 + 164.20) / 21,300 = $0.010222 per gal
                                                  = $10.22 per 1,000 gal
usage-only water      = (45.53 + 152.91) / 21,300 = $0.009316 per gal
```

Wastewater is billed on the water meter's gallons, so the combined figure is the
real cost of running a tap.

## 4. Month over month and year over year

For a figure `A` this period and `B` in the earlier one:

```
change          = A - B
percent change  = (A - B) / |B| × 100        undefined when B = 0
```

Month over month compares the last complete calendar month with the one before
it. Year over year compares it with the same month a year earlier. A month is
complete once it has ended and at least 95% of its hours have data from every
billed meter. Because months differ in length, cost per day is compared too:

```
per day = total / (real hours in the period / 24)
```

That is the number of days, except in a month with a clock change: November
counts as 30.04 days and March as 30.96.

**Example:** August 2026 at 2 kWh and 25 gal an hour, September at 2.5 kWh and
30 gal an hour.

```
kWh      August 2 × 744 = 1,488     September 2.5 × 720 = 1,800
         (1,800 - 1,488) / 1,488 × 100                     = +21.0%
total    August $391.34             September $443.57
         (443.57 - 391.34) / 391.34 × 100                  = +13.3%
per day  391.34 / 31 = $12.62       443.57 / 30 = $14.79   = +17.1%
```

### Why the cost changed

The change in each usage-based cost is split into a usage part and a rate part.
With quantity `Q`, usage-based cost `V` and average rate `p = V / Q`, and the
earlier period marked `0`:

```
usage effect = (Q - Q0) × p0       more or fewer units, at the old average rate
rate effect  = (p - p0) × Q        a different average rate, on the new quantity
usage effect + rate effect = V - V0
```

The fixed-charge change is `F - F0`. The five parts (electricity usage and rate,
water usage and rate, fixed charges) add up to the change in total.

**Example,** the same two months:

```
electricity usage = (1,800 - 1,488) × 0.09624                    = +$30.03
electricity rate  = (0.09624 - 0.09624) × 1,800                  =   $0.00
water usage       = (21,600 - 18,600) × (177.82 / 18,600)        = +$28.68
water rate        = (201.68 / 21,600 - 177.82 / 18,600) × 21,600 =  -$4.81
fixed charges     = 68.66 - 70.32                                =  -$1.66
                                                           total = +$52.23
```

The water rate effect is negative because September passed the tier 1 limit:
gallons above it carry no wastewater charge (until 75% of use overtakes the
limit), which lowers the average cost per gallon.

### To date

The current month and year are also compared like for like. With data through
time `t`:

```
month to date: [start of this month, t)   against   the same number of hours
                                                    from the start of last month
               (late in a month longer than the one before, both are cut to the
               length of the shorter month and labelled "first N days")
year to date:  [1 January, t)             against   1 January to the same date
                                                    and time last year
```

Each uses the percent change formula above, and is left empty when either
stretch has less than 95% of its data.

## 5. Forecasting the bill

### Recent daily usage

The level is an exponentially weighted mean of the last 28 full days. A day that
ended `a` days ago (yesterday is `a` = 1) has weight

```
w = 0.5 ^ ((a - 1) / 7)                 half as much for each week older

level L = Σ w × x / Σ w                 x = the day's usage as a 24-hour rate
sigma σ = sqrt( Σ w × (x - L)² / Σ w )
```

A day needs at least 23 hours of data to count, and `x` is its usage × 24 /
its hours with data, so a day with one missing read and the 23- and 25-hour
clock-change days compare evenly. The drift below uses the same `x`.

**Example:** 40 kWh fifteen days ago, 50 kWh eight days ago, 60 kWh yesterday.

```
weights = 0.5^(14/7), 0.5^(7/7), 0.5^0 = 0.25, 0.5, 1
L = (0.25 × 40 + 0.5 × 50 + 1 × 60) / 1.75 = 95 / 1.75      = 54.29 kWh/day
σ = sqrt((0.25 × 14.29² + 0.5 × 4.29² + 1 × 5.71²) / 1.75)  = 7.28 kWh/day
```

### Seasonal drift

Carrying a level forward ignores the season turning. When last year has the
data, the level is scaled by how usage moved over the same days last year:

```
drift D = (last year's mean daily usage from this date to the cycle's end)
          / (last year's level L going into this date)       kept within 0.5 to 2
```

`D` is 1 when last year isn't available.

### This cycle's bill

With `A` the usage measured so far in the cycle, `H` the cycle's hours and `h`
the hours that have data:

```
days to fill  d = (H - h) / 24
expected usage  = A + L × D × d
spread          = 1.2816 × σ × sqrt(d)          80% range if days vary independently
low, high usage = expected ∓ spread             never below A
```

Expected, low and high usage for electricity and water are then each priced with
the whole-cycle bill of section 1, using the winter average for the cycle's
year as `T` when the rates leave it at 0 (and with one set of fixed charges per meter if
the account has more than one meter of a kind). Pricing the cycle's usage as a whole, rather
than scaling up the cost so far, is what puts tier 2 water, the summer
wastewater discount and the full fixed charges in the right place.

**Example:** on 30 October, seven days into the 23 October to 23 November cycle
(`H` = 745, `h` = 168). Measured: 430 kWh and 4,900 gal. Electricity `L` = 58,
`σ` = 6, `D` = 0.90. Water `L` = 690, `σ` = 150, `D` = 1.

```
d            = (745 - 168) / 24                  = 24.04 days
kWh          = 430 + 58 × 0.90 × 24.04           = 1,685.0
gallons      = 4,900 + 690 × 24.04               = 21,488.8
spread       = 1.2816 × 6 × sqrt(24.04)          = 37.7 kWh
               1.2816 × 150 × sqrt(24.04)        = 942.6 gal

electricity  = 1,685.0 × 0.09624 + 13.00         = $175.16
water        = 20,470 × 0.00209 + 1,018.8 × 0.00331 + 7.99 = $54.14
wastewater   = max(20,470, 0.75 × 21,488.8) × 0.00747 + 11.29 = $164.20
garbage                                          = $38.04
forecast                                         = $431.55
range        = $424.80 to $438.30
```

### The next bill

When the same cycle a year earlier is complete, it carries the season:

```
growth g  = (usage in the last 3 complete cycles)
            / (usage in the same 3 cycles a year earlier)     kept within 0.5 to 1.5
expected  = last year's usage in that cycle × g × (days this year / days last year)
```

Otherwise `expected = L × days`. Either way it is priced with section 1 and
given the same spread as above with `d` = the cycle's days.

**Example:** the 23 November to 23 December cycle. A year earlier it used 1,250
kWh and 19,800 gal over the same 30 days. Recent cycles ran 8% above last year
for electricity and 3% below for water.

```
kWh          = 1,250 × 1.08                      = 1,350
gallons      = 19,800 × 0.97                     = 19,206
electricity  = 1,350 × 0.09624 + 13.00           = $142.92
water        = 19,206 × 0.00209 + 7.99           = $48.13    (December: no tiers)
wastewater   = 19,206 × 0.00747 + 11.29          = $154.76   (every gallon)
garbage                                          = $38.04
forecast                                         = $383.85
```

## 6. Unusual days

### Spikes

Each full day's usage `x` is compared with the full days in the 28 before it (at
least 14 are needed):

```
m   = median of the baseline days
MAD = median of |each baseline day - m|
σ   = max(1.4826 × MAD, 0.10 × m)
z   = (x - m) / σ
```

1.4826 × MAD estimates a standard deviation without being stretched by earlier
spikes, which a plain standard deviation would be. The 10% floor stops a very
steady baseline from making ordinary days look extreme. A day is flagged when
all three hold:

```
z ≥ 3.5       and       x ≥ 1.25 × m       and       x - m ≥ 5 kWh or 100 gal
```

**Example:** a day of 86 kWh after these 14 days:
48, 50, 52, 47, 51, 49, 53, 50, 48, 52, 51, 49, 50, 120.

```
m   = 50                       the 120 does not move the median
MAD = 1.5         1.4826 × 1.5 = 2.22, below the floor of 0.10 × 50 = 5.0
σ   = 5.0
z   = (86 - 50) / 5.0 = 7.2    flagged: 7.2 ≥ 3.5, 86 ≥ 62.5, 36 ≥ 5
```

With a more varied baseline (30, 40, 50, 60, 70, 35, 45, 55, 65, 50, 42, 58, 48,
52: `m` = 50, MAD = 8, `σ` = 11.86) the same day scores `z` = 3.04 and is not
flagged, because days like it are within that home's normal range.

### What it cost

```
excess       = x - m
excess cost  = the day's usage-based charges × excess / x
```

**Example:** 63 kWh against a typical 42: excess 21 kWh, and
`63 × 0.09624 × 21 / 63` = **$2.02**. For 600 extra gallons under the tier 1
limit: `600 × (0.00209 + 0.00747)` = **$5.74**.

### Likely cause

The cause is read from when the extra was used. For each hour of the day, extra
usage is the day's usage in that hour minus the baseline's median for that hour
(never below zero).

| Pattern in the extra usage | Reported cause |
|---|---|
| 60% or more in its 3 biggest hours, right after a day with missing data | `meter_catch_up`: delayed reads, not real usage |
| Electricity: the quietest hour is at least double its usual level and at least 0.5 kWh above it, with under half of the extra in its 3 biggest hours | `always_on_load`: strip heat, a heater, a pump, something stuck on |
| Electricity: 60% or more in its 3 biggest hours | `short_heavy_load`: EV charging, oven, dryer |
| Electricity: half or more between noon and 9 pm, May to October | `cooling` |
| Electricity: half or more overnight and morning, November to March | `heating` |
| Water: flow in every hour of the day | `continuous_flow`: a leak |
| Water: 60% or more in its 3 biggest hours | `short_heavy_draw`: irrigation, pool fill |
| Anything else | `broad_increase`: guests, weather |

These are readings of the usage pattern, not measurements of the cause. There is
no weather data in them: the season is taken from the month.

### Leaks

A home normally has hours with no water flow. Water is reported as running
continuously when every hour for 24 hours in a row shows at least 0.5 gal:

```
leak rate = the smallest hourly flow in the run
wasted    = leak rate × 24            gallons per day
cost      = usage-based water charges × wasted / usage,
            both over the run's last 24 hours
```

**Example:** 1 gal/h for three days is 24 gal a day, far too little to show as
a spike in a 340 gal day, and costs `24 × (0.00209 + 0.00747)` = **$0.23** a
day. It is reported once, on the latest day, with the time it began.
