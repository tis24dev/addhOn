import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PLATFORMS, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    if "@" not in email:
        return "***"
    _, domain = email.split("@", 1)
    return f"***@{domain}"


def _redact_title(title: str | None) -> str | None:
    if not title or "@" not in title:
        return title
    prefix, domain_and_suffix = title.rsplit("@", 1)
    open_paren = prefix.rfind("(")
    safe_prefix = prefix[: open_paren + 1] if open_paren >= 0 else ""
    return f"{safe_prefix}***@{domain_and_suffix}"


async def _async_close_client(client) -> None:
    """Chiude HonClient senza mascherare l'errore originale di setup/unload."""
    try:
        await client.async_close()
    except Exception as err:
        _LOGGER.warning("Errore chiusura HonClient: %s", err)


def _remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rimuove dal registry le entità legacy non più fornite dall'integrazione.

    Lo switch "Alimentazione" (unique_id '<id>_power') è stato rimosso nel
    refactor 2.3/2.4: senza questa pulizia resterebbe nel registry come entità
    orfana, in stato 'unavailable' con il badge '?' su ogni elettrodomestico.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    checked = 0
    removed = 0
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        checked += 1
        if (reg_entry.unique_id or "").endswith("_power"):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info("Rimossa entità legacy 'Alimentazione': %s", reg_entry.entity_id)
    _LOGGER.debug(
        "Setup debug: pulizia legacy completata per entry=%s, controllate=%d, rimosse=%d",
        entry.entry_id,
        checked,
        removed,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura l'integrazione Haier hOn partendo da un Config Entry."""
    from .hon_client import HonClient, _requires_reauth

    # FIX: la chiave salvata dal config_flow è "email", non "username"
    email = entry.data.get("email")
    password = entry.data.get("password")

    _LOGGER.debug(
        "Setup debug: avvio setup entry=%s title=%s email=%s platforms=%s scan_interval=%ss",
        entry.entry_id,
        _redact_title(getattr(entry, "title", None)),
        _redact_email(email),
        PLATFORMS,
        SCAN_INTERVAL,
    )

    if not email:
        _LOGGER.error(
            "Credenziali mancanti nel config entry (chiave 'email' assente). "
            "Rimuovi e riconfigura l'integrazione."
        )
        return False

    hon_client = HonClient(email=email, password=password)

    # Setup iniziale di pyhOn in executor (non blocca l'event loop di HA)
    try:
        _LOGGER.debug("Setup debug: eseguo HonClient.setup_sync in executor")
        await hass.async_add_executor_job(hon_client.setup_sync)
        _LOGGER.debug("Setup debug: HonClient.setup_sync completato")
    except asyncio.CancelledError:
        await _async_close_client(hon_client)
        raise
    except Exception as err:
        _LOGGER.error("Impossibile connettersi a hOn: %s", err)
        await _async_close_client(hon_client)
        if _requires_reauth(err):
            raise ConfigEntryAuthFailed(f"Credenziali hOn non valide: {err}") from err
        raise ConfigEntryNotReady(f"Impossibile connettersi a hOn: {err}") from err

    async def async_update_data() -> dict:
        """Recupera i dati aggiornati da tutti i dispositivi hOn."""
        try:
            _LOGGER.debug("Coordinator debug: inizio aggiornamento dati hOn")
            data = await hon_client.async_get_appliances_data()
            summary = [
                {
                    "id": appliance_id,
                    "name": appliance_data.get("name"),
                    "type": appliance_data.get("type"),
                    "attributes": len(appliance_data.get("attributes", {}))
                    if isinstance(appliance_data.get("attributes"), dict)
                    else 0,
                    "settings": len(appliance_data.get("settings", {}))
                    if isinstance(appliance_data.get("settings"), dict)
                    else 0,
                }
                for appliance_id, appliance_data in data.items()
            ]
            _LOGGER.debug(
                "Coordinator debug: aggiornamento dati hOn completato, dispositivi=%d summary=%s",
                len(data),
                summary,
            )
            return data
        except Exception as err:
            _LOGGER.debug("Coordinator debug: aggiornamento dati hOn fallito: %s", err, exc_info=True)
            if _requires_reauth(err):
                raise ConfigEntryAuthFailed(f"Credenziali hOn non valide: {err}") from err
            raise UpdateFailed(f"Errore aggiornamento hOn: {err}") from err

    stored = False
    try:
        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="Haier hOn data",
            update_method=async_update_data,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

        # Primo fetch
        _LOGGER.debug("Setup debug: primo refresh coordinator in avvio")
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug(
            "Setup debug: primo refresh completato, last_update_success=%s dispositivi=%d",
            getattr(coordinator, "last_update_success", None),
            len(coordinator.data) if isinstance(coordinator.data, dict) else 0,
        )
        coordinator.hon_client = hon_client

        # FIX: salva sia il coordinator che il client nella struttura attesa da tutte le piattaforme
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = {
            "coordinator": coordinator,
            "client": hon_client,
        }
        stored = True
        _LOGGER.debug("Setup debug: coordinator e client salvati in hass.data per entry=%s", entry.entry_id)

        # Pulizia entità legacy (es. switch "Alimentazione" rimosso): non deve
        # mai bloccare il setup, quindi assorbiamo eventuali errori del registry.
        try:
            _remove_legacy_entities(hass, entry)
        except Exception as err:
            _LOGGER.debug("Pulizia entità legacy non riuscita: %s", err)

        _LOGGER.debug("Setup debug: forward piattaforme %s", PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("Setup debug: forward piattaforme completato")
    except asyncio.CancelledError:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Errore unload piattaforme dopo setup annullato: %s", err)
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await _async_close_client(hon_client)
        raise
    except Exception:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Errore unload piattaforme dopo setup fallito: %s", err)
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await _async_close_client(hon_client)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica il config entry quando l'integrazione viene disattivata."""
    _LOGGER.debug("Unload debug: scarico entry=%s platforms=%s", entry.entry_id, PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("Unload debug: async_unload_platforms risultato=%s", unload_ok)
    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        client = entry_data.get("client")
        if client is not None:
            _LOGGER.debug("Unload debug: chiudo HonClient per entry=%s", entry.entry_id)
            await _async_close_client(client)
        else:
            _LOGGER.debug("Unload debug: nessun HonClient da chiudere per entry=%s", entry.entry_id)
    return unload_ok
