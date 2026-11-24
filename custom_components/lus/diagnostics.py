"""Diagnostics for LUS, with the account's identity removed.

Downloaded from the integration's page when reporting a problem, so nothing in
it may identify the account or let anyone sign in to it.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import LusConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "account_id", "title", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LusConfigEntry
) -> dict[str, Any]:
    # There is no coordinator while the entry isn't loaded, which is just
    # when diagnostics are wanted: the portal is down or the sign-in failed.
    coordinator = getattr(entry, "runtime_data", None)
    data = coordinator.data if coordinator else None
    insights = data.insights if data else None
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
                # Rates and dates only.
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "state": getattr(getattr(entry, "state", None), "value", None),
        "last_update_success": (
            coordinator.last_update_success if coordinator else None
        ),
        "bills": (
            {
                "current": asdict(data.current),
                "previous": asdict(data.previous),
            }
            if data
            else None
        ),
        "insights": (
            {
                "data_through": insights.data_through,
                "months": [m.as_dict() for m in insights.months],
                "billing_periods": [c.as_dict() for c in insights.cycles],
                "forecast": insights.forecast.as_dict() if insights.forecast else None,
                "forecast_next": (
                    insights.forecast_next.as_dict() if insights.forecast_next else None
                ),
                "anomalies": [a.as_dict() for a in insights.anomalies],
            }
            if insights
            else None
        ),
    }
