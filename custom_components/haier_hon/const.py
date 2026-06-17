"""Costanti per l'integrazione Haier hOn Extended."""

DOMAIN = "haier_hon"

# Piattaforme supportate
PLATFORMS = ["climate", "sensor", "binary_sensor", "switch", "select", "button"]

# Intervallo di aggiornamento in secondi
# NOTA: il setup iniziale + primo fetch impiega ~22s su cloud lento.
# 60s garantisce margine sufficiente senza stressare l'API Haier.
SCAN_INTERVAL = 60

# Tipi di appliance hOn
APPLIANCE_AC = "AC"       # Condizionatore
APPLIANCE_WM = "WM"       # Lavatrice (Washing Machine)
APPLIANCE_TD = "TD"       # Asciugatrice (Tumble Dryer)
APPLIANCE_WD = "WD"       # Lavasciuga

# ─── Tier 2: tipi read-only ───────────────────────────────────────────────────
# Tipi aggiuntivi esposti come sensori in sola lettura. I parametri provengono
# dalla mappatura della app ufficiale ma NON sono validati su device reali
# (nessuno tra i dispositivi di test è di questi tipi): per questo i sensori di
# questi tipi sono CAPABILITY-GATED (vedi sensor.py / binary_sensor.py), così
# compaiono solo se il device riporta davvero l'attributo. Alcuni codici sono
# alias dello stesso set (FR/FRE come REF, HOB come IH) perché a seconda del
# modello/enroll il cloud può restituire l'uno o l'altro.
APPLIANCE_REF = "REF"     # Frigorifero / frigo-congelatore
APPLIANCE_FR  = "FR"      # Frigo (alias mappa icone)
APPLIANCE_FRE = "FRE"     # Congelatore
APPLIANCE_OV  = "OV"      # Forno
APPLIANCE_DW  = "DW"      # Lavastoviglie
APPLIANCE_WC  = "WC"      # Cantinetta vino
APPLIANCE_IH  = "IH"      # Piano cottura a induzione
APPLIANCE_HOB = "HOB"     # Piano cottura (alias)
APPLIANCE_HO  = "HO"      # Cappa
APPLIANCE_KT  = "KT"      # Macchina caffè / bollitore
APPLIANCE_WH  = "WH"      # Scaldabagno
APPLIANCE_RVC = "RVC"     # Robot aspirapolvere

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

# Service per cambiare a runtime il livello di log del canale MQTT realtime di
# pyhOn. Di default il rumore dei tentativi di riconnessione è silenziato (vedi
# logging_utils); questo service lo riattiva on-demand per il debug. Il nome dei
# logger e la mappa dei livelli vivono in logging_utils.py (testabile in isolamento).
SERVICE_SET_MQTT_LOG_LEVEL = "set_mqtt_log_level"

# Service per alzare/abbassare a runtime il debug dell'integrazione e dei logger
# pyhOn utili alla discovery/polling. MQTT resta gestito dal service dedicato
# sopra per non riaccendere il rumore realtime quando si indaga una lista device
# vuota.
SERVICE_SET_LOG_LEVEL = "set_log_level"
ATTR_LEVEL = "level"

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
# Swing verticale. windDirectionVertical è un ENUM di POSIZIONI, non un bool:
# 2,4,5,6,7 = posizioni fisse del deflettore, 8 = SWING (oscillazione). Il device
# riporta 0 da spento: 0 NON è tra gli enumValues, quindi inviarlo fa sollevare
# ValueError al setter enum di pyhОn e l'API lo rifiuta — è la causa per cui lo
# swing era stato disabilitato. Il fix (climate.py): non inviare MAI 0 (sanitazione
# pre-send) e impostare windDirectionVertical solo a valori ammessi. Gli allowed
# values reali sono letti a runtime da .values del parametro (per-device), con
# windDirectionVerticalPositionSequence come sorgente sul device.
AC_ATTR_SWING_V      = "settings.windDirectionVertical"
AC_ATTR_SWING_H      = "settings.windDirectionHorizontal"
AC_SWING_V_PARAM     = "windDirectionVertical"   # nome param nel comando "settings"
AC_SWING_H_PARAM     = "windDirectionHorizontal"
AC_SWING_V_ON        = "8"                        # 8 = oscillazione verticale
AC_SWING_MODE_ON     = "on"
AC_SWING_MODE_OFF    = "off"
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
# Qualità aria (attributi diretti, confermati sull'AC di Roberto)
AC_ATTR_PM25        = "pm2p5ValueIndoor"   # PM2.5 interno (µg/m³)
AC_ATTR_CO2         = "co2ValueIndoor"     # CO2 interna (ppm)
AC_ATTR_CH2O        = "ch2oValueIndoor"    # formaldeide interna (mg/m³)

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

# ─── Attributi asciugatrice (TD) ──────────────────────────────────────────────
# L'asciugatrice NON espone totalWashCycle; il contatore cicli arriva da
# programsCounter (container statistics). Confermato sul device HD100-C367GU1-IT.
TD_ATTR_CYCLES = "programsCounter"

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

# ─── Sensori/binary aggiuntivi gruppo lavaggio ────────────────────────────────
# Chiavi CONFERMATE live sui device di Roberto: lavatrice HW80-B14959TU1IT e
# asciugatrice HD100-C367GU1-IT. Sono attributi diretti (non in settings).
WM_ATTR_DIRT_LEVEL       = "dirtyLevel"          # livello sporco selezionato (1..3)
WM_ATTR_DRY_LEVEL        = "dryLevel"            # livello asciugatura (WD/TD)
WM_ATTR_LOADING          = "loadingPercentage"  # % carico cestello
WM_ATTR_DELAY            = "delayTime"           # ritardo avvio impostato (minuti)
# Binary sensor (0/1). Porta/blocco oblò già definiti sopra: WM_ATTR_DOOR_OPEN
# (doorStatus, porta aperta) e WM_ATTR_DOOR (doorLockStatus, oblò bloccato).
WM_ATTR_CHILD_LOCK       = "lockStatus"          # blocco comandi (sicurezza bambini)
WM_ATTR_DRUM_CLEAN       = "drumCleaning"        # ciclo pulizia cestello consigliato
WM_ATTR_FILTER_CLEAN     = "filterCleaning"      # pulizia filtro consigliata
WM_ATTR_DRY_CLEAN_NEEDED = "dryCleaningNeeded"   # pulizia condensatore consigliata

# Fase ciclo (prPhase, attributo grezzo numerico). Le mappe traducono prPhase ->
# etichetta della fase; lavatrice/lavasciuga e asciugatrice usano tabelle
# distinte. Valori non in mappa -> "Fase N".
WASHING_PHASE_MAP = {
    "0": "Pronto",
    "1": "Lavaggio",
    "2": "Lavaggio",
    "3": "Salto fase",
    "4": "Risciacquo",
    "5": "Risciacquo",
    "6": "Risciacquo",
    "7": "Asciugatura",
    "8": "Salto fase",
    "9": "Vapore",
    "10": "Pronto",
    "11": "Centrifuga",
    "12": "Pesatura",
    "14": "Lavaggio",
    "15": "Lavaggio",
    "16": "Lavaggio",
    "20": "Avvio rotazione",
    "24": "Rinfresco",
}
TUMBLE_DRYER_PHASE_MAP = {
    "0": "Pronto",
    "1": "Riscaldamento",
    "2": "Asciugatura",
    "3": "Raffreddamento",
    "13": "Raffreddamento",
    "14": "Riscaldamento",
    "15": "Riscaldamento",
    "16": "Raffreddamento",
    "18": "Rotazione",
    "19": "Asciugatura",
    "20": "Asciugatura",
}

# ─── Mappe value→label per i tipi Tier 2 (read-only) ─────────────────────────
# Decodifiche degli enum hOn per i sensori dei tipi aggiuntivi. Valori non in
# mappa -> stringa di fallback (gestita dalle value_fn in sensor.py).

# machMode autoritativo della app (0-10), usato dai tipi che condividono
# MachineMode (forno, lavastoviglie, ...). NOTA: è distinto da WM_STATE_MAP, che
# resta una mappa 0-7 storica locale del gruppo lavaggio e non va modificata.
MACHINE_MODE_MAP = {
    "0": "Inattivo",
    "1": "Selezione",
    "2": "In esecuzione",
    "3": "In pausa",
    "4": "Avvio ritardato",
    "5": "Avvio ritardato (in corso)",
    "6": "Errore",
    "7": "Terminato",
    "8": "Test",
    "9": "Arresto",
    "10": "Mantieni fresco",
}

# Livello sale / brillantante lavastoviglie (saltStatus / rinseAidStatus).
DW_LEVEL_MAP = {
    "0": "OK",
    "1": "Basso",
    "2": "Critico",
    "3": "Non presente",
}

# Fase scaldabagno (prPhase -> EnumWaterHeaterPhase ridotta).
WH_PHASE_MAP = {
    "0": "Pronto",
    "1": "Riscaldamento",
    "2": "Mantenimento",
}

# Stato robot aspirapolvere (prPhase/machMode -> RVCMachModes).
RVC_STATE_MAP = {
    "0": "In attesa",
    "1": "Pulizia automatica",
    "2": "Pulizia localizzata",
    "3": "In pausa",
    "4": "Full & Go",
    "5": "Pulizia completata",
    "6": "In carica",
}

# Potenza di aspirazione robot (power).
RVC_POWER_MAP = {
    "0": "Auto",
    "1": "Turbo",
    "2": "Silenzioso",
}
