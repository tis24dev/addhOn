"""Invio condiviso del comando `settings` del condizionatore.

Sia l'entità climate (modalità/temp/ventola/swing) sia gli switch AC modificano
il MEDESIMO comando pyhОn `settings`, che all'invio trasmette TUTTI i suoi
parametri. Quindi ogni invio deve applicare la stessa sanitazione di
`windDirectionVertical`/`windDirectionHorizontal`: il device può riportarli a 0
(valore non ammesso dagli enumValues) e l'API rifiuterebbe il comando.
Centralizzare qui evita divergenze tra climate.py e switch.py.
"""
from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Parametri direzione-aria che possono valere 0 (device spento): da sanare.
AC_WIND_DIR_PARAMS = ("windDirectionVertical", "windDirectionHorizontal")
AC_SWING_V_PARAM = "windDirectionVertical"
AC_SWING_V_ON = "8"  # 8 = oscillazione verticale


def settings_param(appliance, name):
    """Ritorna il parametro `name` del comando `settings`, o None se assente."""
    commands = getattr(appliance, "commands", None)
    commands = commands if isinstance(commands, dict) else {}
    settings = commands.get("settings")
    params = getattr(settings, "parameters", None) if settings is not None else None
    if isinstance(params, dict):
        return params.get(name)
    return None


def param_allowed_values(param) -> list[str]:
    """Allowed values (come stringhe) di un parametro enum, o [] se non enum."""
    values = getattr(param, "values", None)
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


def fixed_vertical_value(allowed: list[str]) -> str:
    """Posizione verticale FISSA (non-swing) tra quelle ammesse; mai 0."""
    fixed = [v for v in allowed if v != AC_SWING_V_ON]
    if "2" in fixed:
        return "2"
    return fixed[0] if fixed else AC_SWING_V_ON


def sanitize_wind_direction(command_params: dict) -> None:
    """Riporta windDirectionVertical/Horizontal a un valore ammesso se quello
    corrente non lo è (es. 0 da spento). Non tocca i parametri già validi."""
    for key in AC_WIND_DIR_PARAMS:
        param = command_params.get(key)
        if param is None:
            continue
        allowed = param_allowed_values(param)
        if not allowed:
            continue
        current = str(getattr(param, "value", ""))
        if current in allowed:
            continue
        safe = (
            fixed_vertical_value(allowed)
            if key == AC_SWING_V_PARAM
            else next((v for v in allowed if v != "0"), allowed[0])
        )
        try:
            param.value = safe
            _LOGGER.debug(
                "AC settings: sanato %s da %r a %s (ammessi=%s)", key, current, safe, allowed
            )
        except Exception as err:  # pragma: no cover - difensivo
            _LOGGER.warning(
                "AC settings: impossibile sanare %s (valore %r): %s", key, current, err
            )


async def async_send_settings(hass, client, appliance, params: dict) -> None:
    """Applica `params` al comando `settings` e lo invia sul loop dedicato pyhОn.

    Sana windDirection* prima dell'invio (mai 0) e fa rollback dei valori se
    l'assegnazione di un parametro fallisce.
    """
    if not appliance or not client:
        raise HomeAssistantError("AC: appliance o client non disponibile")

    def _do_send():
        async def _inner():
            commands = getattr(appliance, "commands", None)
            commands = commands if isinstance(commands, dict) else {}
            command = commands.get("settings")
            if command is None:
                raise RuntimeError("Comando 'settings' non trovato sul dispositivo AC")
            command_params = getattr(command, "parameters", {})
            missing = [key for key in params if key not in command_params]
            if missing:
                raise RuntimeError(
                    "Parametro/i non trovato/i nel comando settings: " + ", ".join(missing)
                )
            # Sana eventuali windDirection* stantii PRIMA di applicare i parametri
            # richiesti: i valori richiesti vincono comunque.
            sanitize_wind_direction(command_params)
            previous = {}
            assigned = []
            try:
                for key, value in params.items():
                    previous[key] = command_params[key].value
                    assigned.append(key)
                    command_params[key].value = value
                    _LOGGER.debug(
                        "AC settings: '%s' = %s (previous=%s)", key, value, previous[key]
                    )
            except Exception:
                for key in reversed(assigned):
                    try:
                        command_params[key].value = previous[key]
                    except Exception as rollback_err:  # pragma: no cover - difensivo
                        _LOGGER.warning(
                            "AC settings: rollback '%s' fallito: %s", key, rollback_err
                        )
                raise
            await command.send()
            _LOGGER.debug("AC settings: send completato (params=%s)", list(params))

        client.run_command_sync(_inner())

    await hass.async_add_executor_job(_do_send)
