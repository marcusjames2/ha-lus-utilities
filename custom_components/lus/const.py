"""Constants for the LUS integration."""

from datetime import timedelta

DOMAIN = "lus"

UPDATE_INTERVAL = timedelta(hours=6)

# How far back the first import reaches when the meter's first AMI read is unknown.
DEFAULT_BACKFILL = timedelta(days=730)

# Days requested per reads call during backfill.
CHUNK_DAYS = 30
