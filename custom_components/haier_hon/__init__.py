import logging
from datetime import timedelta
import asyncio
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["climate", "sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura l'integrazione Haier hOn partendo da un Config Entry."""
    # Importiamo il client API corretto (adattalo se la classe ha un nome diverso nel tuo file api.py o hon_client.py)
    try:
        from .api import HonApiClient
    except ImportError:
        from .hon_client import HonApiClient

    # Inizializziamo il client API con le credenziali del config entry
    api_client = HonApiClient(entry.data.get("username"), entry.data.get("password"))

    async def async_update_data():
        """Funzione interna che esegue l'unica chiamata di rete per tutti."""
        try:
            async with asyncio.timeout(10):
                devices = await api_client.get_devices()
                if not devices:
                    raise UpdateFailed("Nessun dispositivo restituito dall'API")
                
                # Mappiamo i dispositivi per applianceId per un accesso rapido
                return {device.get("applianceId"): device for device in devices if device.get("applianceId")}
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"Errore di comunicazione con l'API hOn: {err}") from err
        except Exception as ex:
            raise UpdateFailed(f"Errore imprevisto nel coordinatore Haier: {ex}") from ex

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Haier hOn data",
        update_method=async_update_data,
        update_interval=timedelta(seconds=30), # Una chiamata ogni 30 secondi per TUTTI i sensori
    )

    # Inseriamo l'api_client dentro l'oggetto coordinator così le entità possono usarlo per i comandi
    coordinator.api_client = api_client

    # Primo scaricamento dati all'avvio
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault("haier_hon", {})
    hass.data["haier_hon"][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica il config entry quando l'integrazione viene disattivata."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data["haier_hon"].pop(entry.entry_id)
    return unload_ok