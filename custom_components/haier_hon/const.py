"""Costanti per l'integrazione Haier hOn Extended."""

DOMAIN = "haier_hon"

# Piattaforme supportate
PLATFORMS = ["climate", "sensor", "switch", "select", "button"]

# Intervallo di aggiornamento in secondi
# NOTA: il setup iniziale + primo fetch impiega ~22s su cloud lento.
# 60s garantisce margine sufficiente senza stressare l'API Haier.
SCAN_INTERVAL = 60

# Tipi di appliance hOn
APPLIANCE_AC = "AC"       # Condizionatore
APPLIANCE_WM = "WM"       # Lavatrice (Washing Machine)
APPLIANCE_TD = "TD"       # Asciugatrice (Tumble Dryer)
APPLIANCE_WD = "WD"       # Lavasciuga

# Raggruppa tutti gli elettrodomestici lavatrice/asciugatrice/lavasciuga
APPLIANCE_WASH_GROUP = (APPLIANCE_WM, APPLIANCE_TD, APPLIANCE_WD)

# Nomi dei parametri che, nei comandi hOn, contengono il codice/nome del
# programma. Condivisi tra il select (sorgente opzioni + scelta) e il button
# "Avvia programma" (applica il programma scelto a startProgram).
PROGRAM_PARAM_NAMES = ("program", "prCode")

# Chiave dello store volatile (tenuto sul coordinator) che conserva il programma
# scelto dal select ma non ancora avviato; il button "Avvia programma" lo applica
# a startProgram. Unica fonte di verità condivisa tra select.py e button.py.
PROGRAM_PENDING_STORE = "pending_programs"

# ─── Attributi condizionatore ─────────────────────────────────────────────────
# Confermati dai diagnostics del device AS35PBPHRA-PRE
AC_ATTR_MODE         = "settings.machMode"
AC_ATTR_TEMP         = "settings.tempSel"
# tempIndoor / tempOutdoor sono attributi DIRETTI (non in settings) — confermato da diagnostics
AC_ATTR_CURRENT_TEMP     = "tempIndoor"
AC_ATTR_OUTDOOR_TEMP     = "tempOutdoor"
AC_ATTR_HUMIDITY_INDOOR  = "humidityIndoor"          # Umidità ambiente (lettura sensore)
AC_ATTR_HUMIDITY_SEL     = "settings.humiditySel"   # Umidità target (setpoint utente)
AC_ATTR_FAN_SPEED    = "settings.windSpeed"
# DISABILITATO: AC_ATTR_SWING_V e AC_ATTR_SWING_H causano errore in pyhOn quando AC è OFF
# pyhOn tenta di sincronizzare windDirectionVertical=0 che non è permesso (valori ammessi: 2,4,5,6,7,8)
# Issue: https://github.com/telard-pixel/haier_hon/issues/XX
# AC_ATTR_SWING_V      = "settings.windDirectionVertical"
# AC_ATTR_SWING_H      = "settings.windDirectionHorizontal"
AC_ATTR_ON_OFF       = "settings.onOffStatus"
# ecoMode esiste solo in startProgram (NON in settings) — confermato da diagnostics
AC_ATTR_ECO          = "startProgram.ecoMode"
AC_ATTR_RAPID        = "settings.rapidMode"
# silentSleepStatus è il nome reale — muteStatus è separato (muto display)
AC_ATTR_SLEEP        = "settings.silentSleepStatus"
AC_ATTR_SILENT       = "settings.muteStatus"
AC_ATTR_FILTER       = "settings.filterChangeStatusCloud"
AC_ATTR_SELF_CLEAN   = "settings.selfCleaningStatus"
AC_ATTR_LIGHT        = "settings.lightStatus"
AC_ATTR_COMPRESSOR_FREQ = "compressorFrequency"
AC_ATTR_TOTAL_ENERGY = "totalElectricityUsed"

# Mappatura modalità AC -> HA
# Valori accettati dal device: [0, 1, 2, 4, 6]
AC_MODE_MAP = {
    "0": "auto",
    "1": "cool",
    "2": "dry",
    "4": "heat",      # CORRETTO: "4"=CALDO confermato da AS35PBPHRA-PRE
    "6": "fan_only",  # CORRETTO: "6"=VENTILAZIONE confermato da AS35PBPHRA-PRE
}
AC_MODE_MAP_REVERSE = {v: k for k, v in AC_MODE_MAP.items()}

# Fan speed map (confermato: windSpeed in settings)
AC_FAN_MAP = {
    "0": "auto",
    "3": "low",
    "2": "medium",
    "1": "high",
}
AC_FAN_MAP_REVERSE = {v: k for k, v in AC_FAN_MAP.items()}

# ─── Attributi lavatrice ──────────────────────────────────────────────────────
# Confermati dai diagnostics del device HW80-B14959TU1IT
WM_ATTR_STATUS        = "machMode"
WM_ATTR_REMAINING     = "remainingTimeMM"
WM_ATTR_PROGRAM       = "prCode"
WM_ATTR_PROGRAM_NAME  = "programName"              # Nome testuale programma (es. "Cotone")
WM_ATTR_PROGRAM_PHASE = "prPhase"                  # Fase ciclo (prewash/wash/rinse/spin)
WM_ATTR_TEMP          = "temp"                     # CORRETTO: "tempLevel" NON esiste sul device
WM_ATTR_SPIN_SPEED    = "spinSpeed"
WM_ATTR_TOTAL_WASH    = "totalWashCycle"
WM_ATTR_TOTAL_WATER   = "totalWaterUsed"
WM_ATTR_TOTAL_ENERGY  = "totalElectricityUsed"
WM_ATTR_CURRENT_ENERGY = "currentElectricityUsed"  # Energia ciclo in corso
WM_ATTR_CURRENT_WATER  = "currentWaterUsed"         # Acqua ciclo in corso
WM_ATTR_ON_OFF        = "onOffStatus"
WM_ATTR_DOOR          = "doorLockStatus"            # Blocco porta (0=unlocked, 1=locked)
WM_ATTR_DOOR_OPEN     = "doorStatus"                # Porta fisica (0=chiusa, 1=aperta)
WM_ATTR_ERRORS        = "errors"

# ─── Stati lavatrice / asciugatrice ──────────────────────────────────────────
WM_STATE_MAP = {
    "0": "In attesa",
    "1": "In esecuzione",
    "2": "In pausa",
    "3": "Completato",
    "4": "Errore",
    "5": "Programmato",
    "6": "Ritardo avvio",
    "7": "Mezzo carico",
}
