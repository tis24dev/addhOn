import logging
import asyncio
from pyhon import Hon

_LOGGER = logging.getLogger(__name__)

class HonApiClient:
    """Client per comunicare con l'API Cloud hOn di Haier tramite pyhOn."""

    def __init__(self, username, password):
        self._username = username
        self._password = password
        self._hon = None

async def get_appliances(self):
    """Esegue il login (se necessario) e recupera tutti i dispositivi."""
    try:
        if self._hon is None:
            # Inizializza la libreria pyhOn ed effettua il login ai server Haier
            self._hon = await Hon(self._username, self._password)
            await self._hon.setup()

        # pyhOn restituisce un dizionario o una lista di oggetti Appliance
        # Li convertiamo in dizionari nativi o passiamo gli oggetti in base a come serve
        appliances = []
        for appliance in self._hon.appliances:
            # Costruiamo la struttura dati che si aspettano climate.py e sensor.py
            # Estrando i dati dello shadow profile fornito da pyhOn
            appliance_data = {
                "applianceId": appliance.info.get("applianceId"),
                "shadow": {
                    "parameters": {
                        "onOffStatus": {"value": int(appliance.get("onOffStatus", 0))},
                        "machMode": {"value": int(appliance.get("machMode", 1))},
                        "tempSel": {"value": float(appliance.get("tempSel", 24))},
                        "compressorFrequency": {"value": float(appliance.get("compressorFrequency", 0))},
                        "tempIndoor": {"value": float(appliance.get("tempIndoor", 20))},
                        "tempOutdoor": {"value": float(appliance.get("tempOutdoor", 20))},
                    }
                }
            }
            appliances.append(appliance_data)
            
        return appliances
    except Exception as err:
        _LOGGER.error("Errore nel recupero dei dispositivi hOn reali: %s", err)
        raise err

    async def set_device_status(self, appliance_id, parameters: dict):
        """Invia i comandi (es. accensione o cambio temperatura) tramite pyhOn."""
        try:
            if self._hon is None:
                return False
                
            # Cerca il dispositivo corretto all'interno della sessione pyhOn
            for appliance in self._hon.appliances:
                if appliance.info.get("applianceId") == appliance_id:
                    # Invia i parametri aggiornati al cloud
                    for key, value in parameters.items():
                        await appliance.set_parameter(key, value)
                    return True
            return False
        except Exception as err:
            _LOGGER.error("Impossibile inviare il comando al dispositivo %s: %s", appliance_id, err)
            raise err