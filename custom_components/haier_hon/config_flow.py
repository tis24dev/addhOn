"""Config flow per Haier hOn Extended."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
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
    """Valida le credenziali hOn."""
    _LOGGER.debug("ConfigFlow debug: inizio validazione account %s", _redact_email(data.get("email")))
    client = HonClient(email=data["email"], password=data["password"])

    try:
        try:
            # pyhOn esegue operazioni sincrone in __init__/__aenter__ → usa executor
            _LOGGER.debug("ConfigFlow debug: setup_sync in executor")
            await hass.async_add_executor_job(client.setup_sync)
            await client.async_complete_setup()
            _LOGGER.debug("ConfigFlow debug: setup client completato")
        except ImportError as err:
            raise CannotConnect("pyhOn non installato") from err
        except Exception as err:
            _LOGGER.error("Errore validazione: %s", err)
            if _requires_reauth(err):
                raise InvalidAuth(str(err)) from err
            raise CannotConnect(str(err)) from err

        try:
            _LOGGER.debug("ConfigFlow debug: recupero appliance per validazione")
            appliances = await client.async_get_appliances()
            _LOGGER.debug(
                "ConfigFlow debug: appliance recuperate=%d types=%s",
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
            _LOGGER.error("Errore recupero appliance durante validazione: %s", err)
            if _requires_reauth(err):
                raise InvalidAuth(str(err)) from err
            raise CannotConnect(str(err)) from err
    finally:
        try:
            _LOGGER.debug("ConfigFlow debug: chiusura client dopo validazione")
            await client.async_close()
        except Exception as err:
            _LOGGER.warning("Errore chiusura HonClient dopo validazione: %s", err)

    return {
        "title": f"Haier hOn ({data['email']})",
        "appliance_count": len(appliances),
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce il config flow per Haier hOn Extended."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Gestisce il primo step dell'utente."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _LOGGER.debug(
                "ConfigFlow debug: submit step user per account %s",
                _redact_email(user_input.get("email")),
            )
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                _LOGGER.debug("ConfigFlow debug: validazione fallita cannot_connect")
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug("ConfigFlow debug: validazione fallita invalid_auth")
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Errore imprevisto")
                errors["base"] = "unknown"
            else:
                # Evita entry duplicate per stesso account
                await self.async_set_unique_id(user_input["email"].lower())
                self._abort_if_unique_id_configured()

                _LOGGER.debug(
                    "ConfigFlow debug: creo entry per account %s appliance_count=%s",
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
                "docs_url": "https://github.com/telard-pixel/haier_hon"
            },
        )


class CannotConnect(HomeAssistantError):
    """Errore di connessione."""


class InvalidAuth(HomeAssistantError):
    """Credenziali non valide."""
