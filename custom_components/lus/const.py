"""Constants for the LUS integration."""

from datetime import date, timedelta

DOMAIN = "lus"

UPDATE_INTERVAL = timedelta(hours=6)

# How far back the first import reaches when the meter's first AMI read is unknown.
DEFAULT_BACKFILL = timedelta(days=730)

# Days requested per reads call during backfill.
CHUNK_DAYS = 30

# Option holding the date (YYYY-MM-DD) from which stored costs are recomputed on
# the next update, set when rates are saved; cleared once done.
CONF_RECALCULATE_FROM = "recalculate_from"

# How far before the recalculation date to look for the stored running sum.
RECALCULATE_LOOKBACK = timedelta(days=31)

# A recalculation date before any LUS smart-meter data: recomputes every stored cost.
RECALCULATE_ALL = date(2000, 1, 1)
