"""Log in to the LUS portal outside the browser and pull recent reads.

Usage:
    .venv/bin/python scripts/probe.py [days]

Reads LUS_USERNAME / LUS_PASSWORD from the environment, or prompts for them.
Prints meter summaries only; never prints the session token.
"""

import asyncio
from datetime import date, timedelta
from getpass import getpass
import os
from pathlib import Path
import sys

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "lus"))
import api  # noqa: E402


async def main(days: int) -> None:
    username = os.environ.get("LUS_USERNAME") or input("LUS username: ")
    password = os.environ.get("LUS_PASSWORD") or getpass("LUS password: ")

    async with aiohttp.ClientSession() as session:
        client = api.LusClient(session, username, password)
        await client.async_login()
        print("Login OK")

        meters = await client.async_get_meters()
        print(f"{len(meters)} meter(s) with interval data")
        end = date.today()
        start = end - timedelta(days=days)
        for meter in meters:
            reads = await client.async_get_reads(meter, start, end)
            hours = api.to_hourly(reads, meter.reads_per_hour)
            total = sum(h.value for h in hours)
            last = hours[-1].start.isoformat() if hours else "none"
            print(
                f"  {meter.fuel_type:<12} {meter.read_resolution:<12} "
                f"first read {meter.first_read}  "
                f"{len(reads)} reads -> {len(hours)} complete hours, "
                f"total {total:,.3f}, last hour {last}"
            )


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
