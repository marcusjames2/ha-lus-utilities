"""Config flow for LUS."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import LusAuthError, LusClient, LusError
from homeassistant.util import dt as dt_util

from .const import CONF_RECALCULATE_FROM, DOMAIN
from .cost import SITE_TZ, BillingCycles, Rates, RateSchedule, cycle_bounds

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}
)


class LusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect portal credentials and check them."""

    VERSION = 1
    # 2: summer months follow LUS schedules W1 and S1. 3: the water tier-1 limit
    # defaults to the meter's winter average. 4: costs cover hours a meter
    # skipped. 5: billing cycles follow each meter's bills. See async_migrate_entry.
    MINOR_VERSION = 5

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LusOptionsFlow:
        return LusOptionsFlow()

    async def _async_validate(self, username: str, password: str) -> tuple[str | None, str | None]:
        """Return (account id, error key)."""
        client = LusClient(async_create_clientsession(self.hass), username, password)
        try:
            await client.async_login()
            meters = await client.async_get_meters()
        except LusAuthError:
            return None, "invalid_auth"
        except (LusError, aiohttp.ClientError, TimeoutError):
            _LOGGER.exception("Could not reach the LUS portal")
            return None, "cannot_connect"
        if not meters:
            return None, "no_meters"
        return client.account_id, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account_id, error = await self._async_validate(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"LUS account {account_id}", data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            account_id, error = await self._async_validate(
                data[CONF_USERNAME], data[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            elif account_id != entry.unique_id:
                return self.async_abort(reason="wrong_account")
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


def _summer_months(value: str) -> str | None:
    """Normalize a comma-separated month list, or None if it isn't one."""
    months = [m for m in str(value).replace(" ", "").split(",") if m]
    if not all(m.isdigit() and 1 <= int(m) <= 12 for m in months):
        return None
    return ",".join(str(int(m)) for m in months)


class LusOptionsFlow(OptionsFlowWithReload):
    """Enter a bill's rates and the date they take effect.

    Each set of rates applies from its date until the next set's, and costs
    from that date on are recomputed on the next update.
    """

    def __init__(self) -> None:
        self._effective_from: date | None = None

    def _schedule(self) -> RateSchedule:
        return RateSchedule.from_options(self.config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the date the rates take effect."""
        if user_input is not None:
            self._effective_from = date.fromisoformat(user_input["effective_from"])
            return await self.async_step_rates()

        schedule = self._schedule()
        # The coordinator knows the account's real bill dates once it has run.
        coordinator = getattr(self.config_entry, "runtime_data", None)
        latest_bill = _previous_cycle_start(
            getattr(coordinator, "cycles", None) or schedule.cycles
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "effective_from", default=latest_bill.isoformat()
                    ): DateSelector()
                }
            ),
            description_placeholders={"periods": _describe(schedule)},
        )

    async def async_step_rates(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the rates, starting from those already in effect on that date."""
        assert self._effective_from is not None
        schedule = self._schedule()
        errors: dict[str, str] = {}
        if user_input is not None:
            months = _summer_months(user_input["wastewater_summer_months"])
            if months is None:
                errors["wastewater_summer_months"] = "invalid_months"
            else:
                user_input["wastewater_summer_months"] = months
                updated = schedule.with_rates(
                    self._effective_from, Rates.from_options(user_input)
                )
                recalculate_from = self._effective_from
                pending = self.config_entry.options.get(CONF_RECALCULATE_FROM)
                if pending:
                    recalculate_from = min(recalculate_from, date.fromisoformat(pending))
                return self.async_create_entry(
                    data={
                        "rate_schedule": updated.as_options(),
                        CONF_RECALCULATE_FROM: recalculate_from.isoformat(),
                    }
                )

        start = self._effective_from
        current = schedule.at(_local_midnight(start)).as_options()
        schema = {}
        for key, value in current.items():
            if key == "billing_day":
                validator = vol.All(vol.Coerce(int), vol.Range(min=0, max=28))
            elif key == "wastewater_summer_months":
                validator = str
            else:
                validator = vol.All(vol.Coerce(float), vol.Range(min=0))
            schema[vol.Required(key, default=value)] = validator
        return self.async_show_form(
            step_id="rates",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), user_input
            ),
            description_placeholders={"effective_from": start.isoformat()},
            errors=errors,
        )


def _local_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=SITE_TZ)


def _previous_cycle_start(cycles: int | BillingCycles) -> date:
    """Start of the most recently closed billing cycle, the one the latest bill covers."""
    current_start, _ = cycle_bounds(dt_util.now(SITE_TZ), cycles)
    previous_start, _ = cycle_bounds(current_start - timedelta(seconds=1), cycles)
    return previous_start.date()


def _describe(schedule: RateSchedule) -> str:
    """One line per saved set of rates, for the form's description."""
    return "\n".join(
        f"- {f'from {d.isoformat()}' if d else 'from the start of history'}: "
        f"electric ${r.electric_energy_rate + r.electric_fuel_adjustment_rate:.5f}/kWh"
        for d, r in schedule.periods
    )
