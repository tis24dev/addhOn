"""Config flow for Haier hOn Extended."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_ENABLE_DEBUG, CONF_ENABLE_MQTT_DEBUG, DOMAIN
from .hon_client import HonClient, _requires_reauth

_LOGGER = logging.getLogger(__name__)


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    if "@" not in email:
        return "***"
    _, domain = email.split("@", 1)
    return f"***@{domain}"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the hOn credentials."""
    _LOGGER.debug("ConfigFlow debug: starting validation for account %s", _redact_email(data.get("email")))
    client = HonClient(email=data["email"], password=data["password"])

    try:
        try:
            # The client runs synchronous operations in __init__/__aenter__ -> use executor
            _LOGGER.debug("ConfigFlow debug: setup_sync in executor")
            await hass.async_add_executor_job(client.setup_sync)
            await client.async_complete_setup()
            _LOGGER.debug("ConfigFlow debug: client setup completed")
        except ImportError as err:
            raise CannotConnect("required dependency not installed") from err
        except Exception as err:
            _LOGGER.error("Validation error: %s", err)
            if _requires_reauth(err):
                raise InvalidAuth(str(err)) from err
            raise CannotConnect(str(err)) from err

        try:
            _LOGGER.debug("ConfigFlow debug: fetching appliances for validation")
            appliances = await client.async_get_appliances()
            _LOGGER.debug(
                "ConfigFlow debug: appliances fetched=%d types=%s",
                len(appliances),
                [
                    str(getattr(appliance, "appliance_type", None)
                        or getattr(appliance, "applianceType", None)
                        or getattr(appliance, "type_name", None)
                        or getattr(appliance, "category", None)
                        or "UNKNOWN").upper()
                    for appliance in appliances
                ],
            )
        except Exception as err:
            _LOGGER.error("Error fetching appliances during validation: %s", err)
            if _requires_reauth(err):
                raise InvalidAuth(str(err)) from err
            raise CannotConnect(str(err)) from err
    finally:
        try:
            _LOGGER.debug("ConfigFlow debug: closing client after validation")
            await client.async_close()
        except Exception as err:
            _LOGGER.warning("Error closing HonClient after validation: %s", err)

    return {
        "title": f"Haier hOn ({data['email']})",
        "appliance_count": len(appliances),
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handles the config flow for Haier hOn Extended."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "OptionsFlowHandler":
        """Expose the Options flow (the two debug toggles)."""
        # NB: no @callback here so as not to depend on homeassistant.core.callback
        # (not required for correctness; the test harness does not provide it).
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the first user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug(
                "ConfigFlow debug: submit user step for account %s",
                _redact_email(user_input.get("email")),
            )
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                _LOGGER.debug("ConfigFlow debug: validation failed cannot_connect")
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug("ConfigFlow debug: validation failed invalid_auth")
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"
            else:
                # Avoid duplicate entries for the same account
                await self.async_set_unique_id(user_input["email"].lower())
                self._abort_if_unique_id_configured()

                _LOGGER.debug(
                    "ConfigFlow debug: creating entry for account %s appliance_count=%s",
                    _redact_email(user_input.get("email")),
                    info.get("appliance_count"),
                )
                return self.async_create_entry(
                    title=info["title"],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "docs_url": "https://github.com/tis24dev/addhOn"
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Start re-authentication when the hOn token is no longer valid."""
        _LOGGER.debug(
            "ConfigFlow debug: starting reauth for account %s",
            _redact_email(entry_data.get("email")),
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the password again (the email stays the entry's one)."""
        errors: dict[str, str] = {}
        reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        email = reauth_entry.data["email"]

        if user_input is not None:
            data = {"email": email, "password": user_input["password"]}
            try:
                await validate_input(self.hass, data)
            except CannotConnect:
                _LOGGER.debug("ConfigFlow debug: reauth failed cannot_connect")
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug("ConfigFlow debug: reauth failed invalid_auth")
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                # The credentials must belong to the same account: the email is
                # not editable by the user, but we verify the unique_id anyway so
                # as not to re-authenticate an entry with a different account.
                await self.async_set_unique_id(email.lower())
                if reauth_entry.unique_id and self.unique_id != reauth_entry.unique_id:
                    return self.async_abort(reason="reauth_account_mismatch")
                _LOGGER.debug(
                    "ConfigFlow debug: reauth succeeded for %s, updating entry",
                    _redact_email(email),
                )
                return self.async_update_reload_and_abort(reauth_entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
            description_placeholders={"email": email},
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Integration options: two independent debug toggles.

    HA 2024.12.0+: do NOT set self.config_entry in __init__ (deprecated and
    injected automatically). The defaults are read from self.config_entry.options
    (False on installations that never saved options). The values are applied on
    the fly by _apply_debug_options via the options update listener: NB the loggers
    are global to the process, so with more than one account the last one that
    changes wins (typical case = single account).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_ENABLE_DEBUG: bool(user_input.get(CONF_ENABLE_DEBUG, False)),
                    CONF_ENABLE_MQTT_DEBUG: bool(
                        user_input.get(CONF_ENABLE_MQTT_DEBUG, False)
                    ),
                },
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_DEBUG,
                        default=options.get(CONF_ENABLE_DEBUG, False),
                    ): bool,
                    vol.Required(
                        CONF_ENABLE_MQTT_DEBUG,
                        default=options.get(CONF_ENABLE_MQTT_DEBUG, False),
                    ): bool,
                }
            ),
        )


class CannotConnect(HomeAssistantError):
    """Connection error."""


class InvalidAuth(HomeAssistantError):
    """Invalid credentials."""
