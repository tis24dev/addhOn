"""Config flow per Haier hOn Extended."""
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

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "OptionsFlowHandler":
        """Espone il flow Opzioni (i due toggle di debug)."""
        # NB: niente @callback qui per non dipendere da homeassistant.core.callback
        # (non richiesto per correttezza; l'harness di test non lo fornisce).
        return OptionsFlowHandler()

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
                "docs_url": "https://github.com/telard-pixel/addhOn"
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Avvia la ri-autenticazione quando il token hOn non è più valido."""
        _LOGGER.debug(
            "ConfigFlow debug: avvio reauth per account %s",
            _redact_email(entry_data.get("email")),
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Chiede di nuovo la password (l'email resta quella dell'entry)."""
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
                _LOGGER.debug("ConfigFlow debug: reauth fallito cannot_connect")
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                _LOGGER.debug("ConfigFlow debug: reauth fallito invalid_auth")
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Errore imprevisto durante reauth")
                errors["base"] = "unknown"
            else:
                # Le credenziali devono appartenere allo stesso account: l'email
                # non è modificabile dall'utente, ma verifichiamo comunque lo
                # unique_id per non riautenticare un'entry con un altro account.
                await self.async_set_unique_id(email.lower())
                if reauth_entry.unique_id and self.unique_id != reauth_entry.unique_id:
                    return self.async_abort(reason="reauth_account_mismatch")
                _LOGGER.debug(
                    "ConfigFlow debug: reauth riuscito per %s, aggiorno entry",
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
    """Opzioni dell'integrazione: due toggle di debug indipendenti.

    HA 2024.11+: NON impostare self.config_entry nell'__init__ (deprecato e
    iniettato automaticamente). I default sono letti da self.config_entry.options
    (False sulle installazioni che non hanno mai salvato opzioni). I valori sono
    applicati a caldo da _apply_debug_options via l'options update listener: NB
    i logger sono globali al processo, quindi con piu' account vince l'ultimo che
    cambia (caso tipico = singolo account).
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
    """Errore di connessione."""


class InvalidAuth(HomeAssistantError):
    """Credenziali non valide."""
