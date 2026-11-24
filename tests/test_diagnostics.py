"""Diagnostics must never carry the sign-in or the account number."""

from datetime import datetime
import json
from types import SimpleNamespace

from custom_components.lus import analytics as an, coordinator as coord
from custom_components.lus.cost import SITE_TZ, Rates, bill_estimates
from custom_components.lus.diagnostics import async_get_config_entry_diagnostics

NOW = datetime(2026, 10, 31, 9, tzinfo=SITE_TZ)


async def test_diagnostics_redact_credentials_and_account():
    current, previous = bill_estimates([], 23, NOW)
    insights = an.build_insights(an.build_ledger([], Rates()), Rates(), NOW)
    entry = SimpleNamespace(
        title="LUS account 5000000000",
        unique_id="5000000000",
        data={"username": "someone@example.com", "password": "hunter2"},
        options={"rate_schedule": [{"effective_from": None, "billing_day": 23}]},
        runtime_data=SimpleNamespace(
            last_update_success=True,
            data=coord.LusData(
                "5000000000", current, previous, coord.WinterAverageData(2026, None, None), insights
            ),
        ),
    )
    result = await async_get_config_entry_diagnostics(None, entry)

    text = json.dumps(result, default=str)
    for secret in ("hunter2", "someone@example.com", "5000000000"):
        assert secret not in text
    assert result["entry"]["data"] == {"username": "**REDACTED**", "password": "**REDACTED**"}
    assert result["entry"]["options"]["rate_schedule"][0]["billing_day"] == 23
    assert result["bills"]["current"]["cost"] == 0
    assert result["insights"]["anomalies"] == []


async def test_diagnostics_before_the_first_update():
    entry = SimpleNamespace(
        title="LUS account 5000000000", unique_id="5000000000",
        data={"username": "u", "password": "p"}, options={},
        runtime_data=SimpleNamespace(last_update_success=False, data=None),
    )
    result = await async_get_config_entry_diagnostics(None, entry)
    assert result["bills"] is None and result["insights"] is None


async def test_diagnostics_when_the_entry_never_loaded():
    # Portal down or sign-in rejected: there is no coordinator at all.
    entry = SimpleNamespace(
        title="LUS account 5000000000", unique_id="5000000000",
        data={"username": "u", "password": "p"}, options={},
    )
    result = await async_get_config_entry_diagnostics(None, entry)
    assert result["last_update_success"] is None and result["bills"] is None
    assert "5000000000" not in json.dumps(result, default=str)
