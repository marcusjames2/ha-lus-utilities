"""Config flow for LUS."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import LusAuthError, LusClient, LusError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


class LusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect portal credentials and check them."""

    VERSION = 1

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
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )
