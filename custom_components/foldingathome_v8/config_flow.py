"""Config flow for Folding@home v8."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DEFAULT_PORT, FahClient, FahConnectionError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
)


class FahConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Folding@home v8 client by address."""

    VERSION = 1

    async def _async_probe(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """Connect once and return the client's info block."""
        client = FahClient(
            async_get_clientsession(self.hass),
            user_input[CONF_HOST].strip(),
            user_input[CONF_PORT],
        )
        try:
            snapshot = await client.connect()
        except FahConnectionError:
            errors["base"] = "cannot_connect"
            return None
        except Exception:
            _LOGGER.exception("Unexpected error while connecting")
            errors["base"] = "unknown"
            return None
        finally:
            await client.close()
        info = snapshot.get("info") or {}
        if not info.get("id"):
            errors["base"] = "cannot_connect"
            return None
        return info

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the address of the client."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            if info := await self._async_probe(user_input, errors):
                await self.async_set_unique_id(info["id"])
                self._abort_if_unique_id_configured(updates=user_input)
                return self.async_create_entry(
                    title=info.get("mach_name") or user_input[CONF_HOST],
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(DATA_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address, for example after a new IP address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            if info := await self._async_probe(user_input, errors):
                await self.async_set_unique_id(info["id"])
                self._abort_if_unique_id_mismatch(reason="wrong_machine")
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA, user_input or entry.data
            ),
            errors=errors,
        )
