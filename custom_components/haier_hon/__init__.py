import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PLATFORMS, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


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
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (reg_entry.unique_id or "").endswith("_power"):
            registry.async_remove(reg_entry.entity_id)
            _LOGGER.info("Rimossa entità legacy 'Alimentazione': %s", reg_entry.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura l'integrazione Haier hOn partendo da un Config Entry."""
    from .hon_client import HonClient, _requires_reauth

    # FIX: la chiave salvata dal config_flow è "email", non "username"
    email = entry.data.get("email")
    password = entry.data.get("password")

    if not email:
        _LOGGER.error(
            "Credenziali mancanti nel config entry (chiave 'email' assente). "
            "Rimuovi e riconfigura l'integrazione."
        )
        return False

    hon_client = HonClient(email=email, password=password)

    # Setup iniziale di pyhOn in executor (non blocca l'event loop di HA)
    try:
        await hass.async_add_executor_job(hon_client.setup_sync)
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
            return await hon_client.async_get_appliances_data()
        except Exception as err:
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
        await coordinator.async_config_entry_first_refresh()
        coordinator.hon_client = hon_client

        # FIX: salva sia il coordinator che il client nella struttura attesa da tutte le piattaforme
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = {
            "coordinator": coordinator,
            "client": hon_client,
        }
        stored = True

        # Pulizia entità legacy (es. switch "Alimentazione" rimosso): non deve
        # mai bloccare il setup, quindi assorbiamo eventuali errori del registry.
        try:
            _remove_legacy_entities(hass, entry)
        except Exception as err:
            _LOGGER.debug("Pulizia entità legacy non riuscita: %s", err)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
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
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        client = entry_data.get("client")
        if client is not None:
            await _async_close_client(client)
    return unload_ok
