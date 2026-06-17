"""Helper condivisi per inviare comandi pyhОn ai controlli (Tier 3).

Generalizza il pattern già usato da button.py (invio di un comando applicando
override di parametri) e da ac_command.async_send_settings (set sul comando di
scrittura), rendendolo neutro rispetto al nome del comando. I controlli dei tipi
Tier 3 (number, switch/select/button per frigo/forno/…) lo riusano senza
duplicare lookup, rollback ed esecuzione sul loop dedicato pyhОn.

Principio di gating (vedi memoria/repo): ogni controllo è CAPABILITY-GATED, cioè
si crea solo se il device espone DAVVERO il comando + parametro (schema runtime
di pyhОn), col superset dei candidati seminato dalla mappatura della app. Così è
validato dove abbiamo il dump reale, ampio per gli altri modelli, e sicuro
ovunque (un parametro assente non genera entità).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
import logging

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Comandi pyhОn da cui i controlli "set" (number/switch/select-modo) leggono e
# scrivono i parametri liberi. pyhОn nomina il comando dalla chiave di primo
# livello del device: "settings" è quella dell'AC e del frigo reale (categoria
# attiva setParameters); "setParameters" come fallback per altri modelli.
SETTINGS_COMMANDS: tuple[str, ...] = ("settings", "setParameters")


def get_commands(appliance) -> dict:
    """Dizionario comandi del device, o {} se assente/non valido."""
    commands = getattr(appliance, "commands", None)
    return commands if isinstance(commands, dict) else {}


def get_command(appliance, name: str):
    """Comando `name`, o None."""
    return get_commands(appliance).get(name)


def command_param(appliance, command_name: str, param_name: str):
    """Parametro `param_name` del comando `command_name`, o None se assente."""
    command = get_command(appliance, command_name)
    params = getattr(command, "parameters", None) if command is not None else None
    if isinstance(params, dict):
        return params.get(param_name)
    return None


def find_settings_param(
    appliance, param_name: str, command_names: Sequence[str] = SETTINGS_COMMANDS
):
    """Cerca `param_name` tra i comandi `command_names` (in ordine).

    Ritorna (command_name, param) del primo match, o None. È il capability-gate
    dei controlli che scrivono su un comando settings/setParameters.
    """
    for name in command_names:
        param = command_param(appliance, name, param_name)
        if param is not None:
            return name, param
    return None


def param_values(param) -> list[str]:
    """Valori ammessi (stringhe) di un parametro enum, o [] se non enumerato."""
    values = getattr(param, "values", None)
    if isinstance(values, (list, tuple)):
        return [str(v) for v in values]
    return []


def param_range(param) -> tuple[float, float, float] | None:
    """(min, max, step) di un parametro range pyhОn, o None se non è un range.

    Duck-typing su min/max/step (HonParameterRange li espone). step torna 1.0 se
    pyhОn lo riporta a 0 (nessun incremento dichiarato)."""
    if not all(hasattr(param, attr) for attr in ("min", "max", "step")):
        return None
    try:
        lo = float(param.min)
        hi = float(param.max)
        step = float(param.step) or 1.0
    except (TypeError, ValueError):
        return None
    if hi < lo:
        return None
    return lo, hi, step


async def async_send_command(
    hass,
    client,
    appliance,
    command_name: str,
    params: dict,
    *,
    pre_send: Callable[[dict], None] | None = None,
) -> None:
    """Applica `params` (nome->valore) al comando `command_name` e lo invia sul
    loop dedicato pyhОn, con rollback se un assegnamento fallisce.

    `pre_send(command_params)`: hook opzionale eseguito PRIMA di applicare i
    parametri richiesti (l'AC lo usa per sanare windDirection*). I valori
    richiesti vincono comunque su ciò che pre_send ha impostato.
    """
    if not appliance or not client:
        raise HomeAssistantError("Comando: appliance o client non disponibile")

    def _do_send():
        async def _inner():
            command = get_command(appliance, command_name)
            if command is None:
                raise RuntimeError(
                    f"Comando '{command_name}' non trovato sul dispositivo"
                )
            command_params = getattr(command, "parameters", {})
            if not isinstance(command_params, dict):
                command_params = {}
            missing = [key for key in params if key not in command_params]
            if missing:
                raise RuntimeError(
                    f"Parametro/i non trovato/i nel comando {command_name}: "
                    + ", ".join(missing)
                )
            if pre_send is not None:
                pre_send(command_params)
            previous: dict = {}
            assigned: list[str] = []
            try:
                for key, value in params.items():
                    previous[key] = command_params[key].value
                    assigned.append(key)
                    command_params[key].value = value
                    _LOGGER.debug(
                        "Comando %s: '%s' = %s (previous=%s)",
                        command_name,
                        key,
                        value,
                        previous[key],
                    )
            except Exception:
                for key in reversed(assigned):
                    try:
                        command_params[key].value = previous[key]
                    except Exception as rollback_err:  # pragma: no cover - difensivo
                        _LOGGER.warning(
                            "Comando %s: rollback '%s' fallito: %s",
                            command_name,
                            key,
                            rollback_err,
                        )
                raise
            await command.send()
            _LOGGER.debug(
                "Comando %s: send completato (params=%s)", command_name, list(params)
            )

        client.run_command_sync(_inner())

    await hass.async_add_executor_job(_do_send)
