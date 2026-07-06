"""Config flow for Haier hOn Extended."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .client.transport.auth import MFAChallengeRequired, MFACodeInvalid
from .const import CONF_ENABLE_DEBUG, CONF_ENABLE_MQTT_DEBUG, DOMAIN
from .error_codes import MFA_CODE_INVALID, UNKNOWN, HonErrorCode, classify
from .hon_client import HonClient, _requires_reauth

_LOGGER = logging.getLogger(__name__)


def _error_base_and_code(exc: BaseException, fallback_base: str) -> tuple[str, str]:
    """Map a validation exception to the (form error key, ADDHON-NNN label).

    A code with a localized ``config.error.<slug>`` string (``ui=True``) drives both
    the precise key AND the shown code; otherwise the generic bucket
    (cannot_connect / invalid_auth) is used with the bare code label. A code-less
    exception (legacy string-constructed CannotConnect/InvalidAuth in the tests)
    falls back to the bucket with no code."""
    code = getattr(exc, "error_code", None)
    if isinstance(code, HonErrorCode):
        if code.ui:
            return code.slug, code.label
        return fallback_base, code.label
    return fallback_base, ""


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
    # validation=True: authenticate + count appliances only, NO MQTT and no
    # per-appliance loads, so a slow/blocked realtime or a single dead endpoint can
    # no longer make the whole validation hit the 60s loop cap (issue #30).
    client = HonClient(email=data["email"], password=data["password"], validation=True)
    mfa_pending = False

    try:
        try:
            # The client runs synchronous operations in __init__/__aenter__ -> use executor
            _LOGGER.debug("ConfigFlow debug: setup_sync in executor")
            await hass.async_add_executor_job(client.setup_sync)
            await client.async_complete_setup()
            _LOGGER.debug("ConfigFlow debug: client setup completed")
        except MFAChallengeRequired as err:
            # 2FA email-OTP: keep the live client (its session holds the challenge) so
            # the flow can drive the 2FA step. Hand it to the caller and SKIP the close
            # in `finally`; the flow handler owns the teardown from here on.
            _LOGGER.debug("ConfigFlow debug: 2FA challenge, deferring to the 2FA step")
            mfa_pending = True
            err.client = client
            raise
        except ImportError as err:
            code = classify(err)
            _LOGGER.error("Validation failed [%s]: required dependency not installed: %s", code.label, err)
            raise CannotConnect(code) from err
        except Exception as err:
            code = classify(err)
            _LOGGER.error("Validation failed [%s]: %s", code.label, err)
            if _requires_reauth(err):
                raise InvalidAuth(code) from err
            raise CannotConnect(code) from err

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
            code = classify(err)
            _LOGGER.error("Validation failed [%s] fetching appliances: %s", code.label, err)
            if _requires_reauth(err):
                raise InvalidAuth(code) from err
            raise CannotConnect(code) from err
        # Capture the refresh token BEFORE the `finally` closes the client (the close
        # nulls the session, after which the property returns ""). Persisting it lets a
        # non-2FA account skip the full login on the next restart too.
        refresh_token = client.refresh_token
    finally:
        # On a 2FA challenge the client must stay open for the 2FA step (the flow
        # handler closes it); otherwise close it here as before.
        if not mfa_pending:
            try:
                _LOGGER.debug("ConfigFlow debug: closing client after validation")
                await client.async_close()
            except Exception as err:
                _LOGGER.warning("Error closing HonClient after validation: %s", err)

    return {
        "title": f"Haier hOn ({data['email']})",
        "appliance_count": len(appliances),
        "refresh_token": refresh_token,
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handles the config flow for Haier hOn Extended."""

    VERSION = 1

    # 2FA (email OTP) state, carried across the user/reauth -> 2fa steps. Class-level
    # defaults so a fresh handler never AttributeErrors; reassigned per-flow in
    # _mfa_begin. _mfa_client is the LIVE validation client whose session holds the
    # challenge (the flow owns its teardown). NEVER logged.
    _mfa_client: HonClient | None = None
    _mfa_context: Any = None
    _mfa_data: dict[str, Any] | None = None
    _mfa_reauth_entry: Any = None

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
        error_code = ""

        if user_input is not None:
            _LOGGER.debug(
                "ConfigFlow debug: submit user step for account %s",
                _redact_email(user_input.get("email")),
            )
            # Set the unique_id and abort BEFORE the network validation, so re-adding
            # an already-configured account is rejected without a costly hOn login +
            # appliance fetch (rate-limited). Must be OUTSIDE the try below: the
            # AbortFlow raised by _abort_if_unique_id_configured() would otherwise be
            # swallowed by the broad `except Exception`. (#18)
            await self.async_set_unique_id(user_input["email"].lower())
            self._abort_if_unique_id_configured()
            try:
                info = await validate_input(self.hass, user_input)
            except MFAChallengeRequired as err:
                # 2FA required: hold the live client and move to the OTP step.
                await self._mfa_begin(err, dict(user_input), reauth_entry=None)
                return await self.async_step_2fa()
            except CannotConnect as err:
                errors["base"], error_code = _error_base_and_code(err, "cannot_connect")
                _LOGGER.debug("ConfigFlow debug: validation failed %s [%s]", errors["base"], error_code)
            except InvalidAuth as err:
                errors["base"], error_code = _error_base_and_code(err, "invalid_auth")
                _LOGGER.debug("ConfigFlow debug: validation failed %s [%s]", errors["base"], error_code)
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"
                error_code = UNKNOWN.label
            else:
                _LOGGER.debug(
                    "ConfigFlow debug: creating entry for account %s appliance_count=%s",
                    _redact_email(user_input.get("email")),
                    info.get("appliance_count"),
                )
                return self.async_create_entry(
                    title=info["title"],
                    data={**user_input, "refresh_token": info.get("refresh_token", "")},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "docs_url": "https://github.com/tis24dev/addhOn",
                "error_code": error_code,
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
        error_code = ""
        reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if reauth_entry is None:
            # The entry was removed while the reauth flow was open: abort cleanly
            # instead of raising AttributeError on reauth_entry.data below. Use a
            # dedicated reason -- reauth_account_mismatch ("credentials must belong to
            # the same account") would be a misleading message for a missing entry.
            return self.async_abort(reason="reauth_entry_not_found")
        email = reauth_entry.data["email"]

        if user_input is not None:
            data = {"email": email, "password": user_input["password"]}
            try:
                info = await validate_input(self.hass, data)
            except MFAChallengeRequired as err:
                # 2FA required during reauth: hold the client and move to the OTP step,
                # which finishes by UPDATING this entry (not creating a new one).
                await self._mfa_begin(err, data, reauth_entry=reauth_entry)
                return await self.async_step_2fa()
            except CannotConnect as err:
                errors["base"], error_code = _error_base_and_code(err, "cannot_connect")
                _LOGGER.debug("ConfigFlow debug: reauth failed %s [%s]", errors["base"], error_code)
            except InvalidAuth as err:
                errors["base"], error_code = _error_base_and_code(err, "invalid_auth")
                _LOGGER.debug("ConfigFlow debug: reauth failed %s [%s]", errors["base"], error_code)
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
                error_code = UNKNOWN.label
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
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**data, "refresh_token": info.get("refresh_token", "")},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
            description_placeholders={"email": email, "error_code": error_code},
        )

    async def _mfa_begin(
        self, err: MFAChallengeRequired, data: dict[str, Any], reauth_entry: Any
    ) -> None:
        """Stash the live 2FA challenge so async_step_2fa can drive it.

        Defensive: if the user/reauth step is re-entered while a previous challenge
        client is still held, close it first so its loop/thread/session is not orphaned
        (the guard avoids closing the just-arrived client when it is the same object)."""
        new_client = err.client
        if self._mfa_client is not None and self._mfa_client is not new_client:
            await self._async_close_mfa_client()
        self._mfa_client = new_client
        self._mfa_context = err.context
        self._mfa_data = data
        self._mfa_reauth_entry = reauth_entry

    async def _async_close_mfa_client(self) -> None:
        """Tear down the held 2FA client. Idempotent (clears the ref first), so it is
        safe to call from both the success path and the flow-removal hook."""
        client = self._mfa_client
        self._mfa_client = None
        self._mfa_context = None
        # Drop the cached form data too: _mfa_data holds the plaintext password and
        # _mfa_reauth_entry the reauth target, so stale credentials/state are not left
        # reachable on the flow object after success, abort, or async_remove.
        self._mfa_data = None
        self._mfa_reauth_entry = None
        if client is not None:
            try:
                await client.async_close()
            except Exception as err:  # noqa: BLE001 - cleanup must not mask the flow
                _LOGGER.warning("Error closing HonClient after 2FA: %s", err)

    async def async_remove(self) -> None:
        """Called by HA when the flow is removed/aborted/abandoned: close the held 2FA
        client so an unfinished verification does not leak its loop/thread/session."""
        await self._async_close_mfa_client()

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and submit the email OTP for a pending 2FA challenge."""
        errors: dict[str, str] = {}
        error_code = ""

        if self._mfa_client is None or self._mfa_context is None:
            # The challenge state is gone (timed out / stale flow): start over.
            return self.async_abort(reason="mfa_no_challenge")

        if user_input is None:
            # First entry: actually SEND the code (loading the page does not email it).
            try:
                await self.hass.async_add_executor_job(
                    self._mfa_client.resend_mfa_code_sync, self._mfa_context
                )
                _LOGGER.debug("ConfigFlow debug: 2FA code sent")
            except Exception as err:  # noqa: BLE001
                errors["base"], error_code = self._mfa_error(err)
        elif user_input.get("resend"):
            try:
                await self.hass.async_add_executor_job(
                    self._mfa_client.resend_mfa_code_sync, self._mfa_context
                )
                _LOGGER.debug("ConfigFlow debug: 2FA code resent")
            except Exception as err:  # noqa: BLE001
                errors["base"], error_code = self._mfa_error(err)
        else:
            code = (user_input.get("code") or "").strip()
            if not code:
                errors["base"], error_code = "mfa_code_invalid", MFA_CODE_INVALID.label
            else:
                try:
                    await self.hass.async_add_executor_job(
                        self._mfa_client.submit_mfa_code_sync, self._mfa_context, code
                    )
                    refresh_token = self._mfa_client.refresh_token
                except MFACodeInvalid as err:
                    errors["base"], error_code = _error_base_and_code(err, "invalid_auth")
                    _LOGGER.debug("ConfigFlow debug: 2FA code rejected [%s]", error_code)
                except Exception as err:  # noqa: BLE001
                    errors["base"], error_code = self._mfa_error(err)
                    _LOGGER.debug("ConfigFlow debug: 2FA submit failed [%s]", error_code)
                else:
                    return await self._async_finish_2fa(refresh_token)

        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema(
                {
                    # Optional (not Required) so the user can submit with the code empty to
                    # trigger "Resend code"; the handler validates non-empty before submit.
                    vol.Optional("code", default=""): str,
                    vol.Optional("resend", default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={"error_code": error_code},
        )

    @staticmethod
    def _mfa_error(err: BaseException) -> tuple[str, str]:
        """(form base, ADDHON label) for a non-code 2FA failure (send/submit)."""
        code = classify(err)
        if code.ui:
            return code.slug, code.label
        return ("invalid_auth" if _requires_reauth(err) else "cannot_connect"), code.label

    async def _async_finish_2fa(self, refresh_token: str) -> FlowResult:
        """Create or update the entry after a successful OTP verification."""
        data = {**(self._mfa_data or {}), "refresh_token": refresh_token}
        email = data.get("email", "")
        reauth_entry = self._mfa_reauth_entry
        await self._async_close_mfa_client()
        if reauth_entry is not None:
            await self.async_set_unique_id(email.lower())
            if reauth_entry.unique_id and self.unique_id != reauth_entry.unique_id:
                return self.async_abort(reason="reauth_account_mismatch")
            _LOGGER.debug("ConfigFlow debug: 2FA reauth succeeded, updating entry")
            return self.async_update_reload_and_abort(reauth_entry, data=data)
        _LOGGER.debug("ConfigFlow debug: 2FA succeeded, creating entry")
        return self.async_create_entry(title=f"Haier hOn ({email})", data=data)


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


class _CodedFlowError(HomeAssistantError):
    """Base for the two flow errors: optionally carries a HonErrorCode.

    Accepts either a HonErrorCode (the new path) or a plain string/None (legacy
    callers and tests). The carried code drives the precise UI key + the shown
    ADDHON-NNN; a string is just the message with no code."""

    def __init__(self, code: HonErrorCode | str | None = None) -> None:
        if isinstance(code, HonErrorCode):
            self.error_code: HonErrorCode | None = code
            super().__init__(str(code))
        else:
            self.error_code = None
            super().__init__("" if code is None else str(code))


class CannotConnect(_CodedFlowError):
    """Connection error."""


class InvalidAuth(_CodedFlowError):
    """Invalid credentials."""
