"""Constants for the Haier hOn Extended integration."""

DOMAIN = "addhon"

# Supported platforms
PLATFORMS = ["climate", "sensor", "binary_sensor", "switch", "select", "button", "number"]

# Update interval in seconds
# NOTE: the initial setup + first fetch takes ~22s on a slow cloud.
# 60s gives enough margin without stressing the Haier API.
SCAN_INTERVAL = 60

# hOn appliance types
APPLIANCE_AC = "AC"       # Air conditioner
APPLIANCE_WM = "WM"       # Washing Machine
APPLIANCE_TD = "TD"       # Tumble Dryer
APPLIANCE_WD = "WD"       # Washer-dryer

# --- Tier 2: read-only types --------------------------------------------------
# Additional types exposed as read-only sensors. The parameters come from the
# official app mapping but are NOT validated on real devices (none of the test
# devices are of these types): for this reason the sensors of these types are
# CAPABILITY-GATED (see sensor.py / binary_sensor.py), so they only show up if the
# device actually reports the attribute. Some codes are aliases of the same set
# (FR/FRE as REF, HOB as IH) because, depending on the model/enroll, the cloud may
# return one or the other.
APPLIANCE_REF = "REF"     # Refrigerator / fridge-freezer
APPLIANCE_FR  = "FR"      # Fridge (icon-map alias)
APPLIANCE_FRE = "FRE"     # Freezer
APPLIANCE_OV  = "OV"      # Oven
APPLIANCE_DW  = "DW"      # Dishwasher
APPLIANCE_WC  = "WC"      # Wine cooler
APPLIANCE_IH  = "IH"      # Induction hob
APPLIANCE_HOB = "HOB"     # Hob (alias)
APPLIANCE_HO  = "HO"      # Hood
APPLIANCE_KT  = "KT"      # Coffee machine / kettle
APPLIANCE_WH  = "WH"      # Water heater
APPLIANCE_RVC = "RVC"     # Robot vacuum cleaner

# Groups all washing machine/tumble dryer/washer-dryer appliances
APPLIANCE_WASH_GROUP = (APPLIANCE_WM, APPLIANCE_TD, APPLIANCE_WD)

# Names of the parameters that, in hOn commands, carry the program code/name.
# Shared between the select (options source + choice) and the "Start program"
# button (applies the chosen program to startProgram).
PROGRAM_PARAM_NAMES = ("program", "prCode")

# Key of the volatile store (kept on the coordinator) that holds the program
# chosen by the select but not yet started; the "Start program" button applies it
# to startProgram. The single shared source of truth between select.py and button.py.
PROGRAM_PENDING_STORE = "pending_programs"

# Key of the volatile store (kept on the coordinator) that holds the writable
# program OPTIONS (spin/temp/dry level/extra rinses/delayed start/...) chosen on the
# option entities but not yet started. Shape: {appliance_id: {param: value_str}}.
# Parallel to PROGRAM_PENDING_STORE: the option entities (switch/select/number) buffer
# here, and the "Start program" button applies them to the startProgram command and
# clears them on a successful send (see program_options.py / button.py). Discussion #35.
PROGRAM_PENDING_OPTIONS = "pending_options"

# --- Program-option label maps (discussion #35) -------------------------------
# Labels ONLY (machine keys for the select state translations); the legal VALUE set is
# ALWAYS read from the device's startProgram schema, never hardcoded (a model exposes
# only a slice -- e.g. the user's dryer shows dryLevel[12,13,14], tempLevel[2,3,4]).
# Values are the decompiled-app codes. dryLevel semantics are NOT stable across classes
# (value 1 = EXTRA_DRY on WM/WD but IRON_DRY on TD), so it is TYPE-GATED with two maps.
DRY_LEVEL_LABELS_WM = {
    "1": "extra_dry",
    "2": "cupboard",
    "3": "iron_dry",
    "4": "hang_dry",
    "5": "smart_dry",
    "6": "wool_dry",
}  # decomp case 300 (WM/WD)
DRY_LEVEL_LABELS_TD = {
    "1": "iron_dry",
    "2": "hang_dry",
    "3": "cupboard",
    "4": "extra_dry",
    "12": "iron_dry",
    "13": "ready_to_wear",
    "14": "cupboard",
    "15": "extra_dry",
}  # decomp case 53 (TD)
TEMP_LEVEL_LABELS = {"1": "minimum", "2": "low", "3": "medium", "4": "high"}
DIRTY_LEVEL_LABELS = {"1": "little", "2": "normal", "3": "very"}
STEAM_LEVEL_LABELS = {"0": "no_steam", "1": "cotton", "2": "delicate", "3": "synthetic"}
# Unselectable dryLevel sentinels (hasDryLevelValue returns false for ''/'0'/'11'):
# dropped from the select options so they never appear as a choice.
DRY_LEVEL_SENTINELS = ("0", "11")

# Service to change at runtime the log level of the realtime MQTT channel. By
# default the reconnection-attempt noise is silenced (see logging_utils); this
# service re-enables it on demand for debugging. The logger names and the level
# map live in logging_utils.py (testable in isolation).
SERVICE_SET_MQTT_LOG_LEVEL = "set_mqtt_log_level"

# Service to raise/lower at runtime the debug of the integration and of the native
# hOn client loggers useful for discovery/polling. MQTT stays handled by the dedicated
# service above so as not to turn the realtime noise back on when investigating an
# empty device list.
SERVICE_SET_LOG_LEVEL = "set_log_level"
ATTR_LEVEL = "level"

# Domain-wide service that forces an immediate cloud poll for ALL loaded config
# entries: the automation-callable equivalent of the per-device "Refresh now"
# button. Global to the domain (no target, no fields), registered once.
SERVICE_REFRESH = "refresh"

# Option keys (entry.options) of the two debug toggles exposed in the
# Configure/Options screen of the integration. They persist across restarts and
# are applied on the fly (see _apply_debug_options in __init__). enable_debug ->
# integration logger to DEBUG (NOTSET when off); enable_mqtt_debug -> realtime MQTT
# logger to DEBUG (silenced to WARNING when off). The two toggles are independent.
CONF_ENABLE_DEBUG = "enable_debug"
CONF_ENABLE_MQTT_DEBUG = "enable_mqtt_debug"

# --- Air conditioner attributes -----------------------------------------------
# Confirmed from the diagnostics of the AS35PBPHRA-PRE device
AC_ATTR_MODE         = "settings.machMode"
AC_ATTR_TEMP         = "settings.tempSel"
# Parameter NAMES inside the "settings" command (write side), distinct from the
# dotted attribute paths above (read side): used to read the device's real
# range/enum schema for the climate entity (setpoint range, hvac/fan modes).
AC_TEMP_PARAM        = "tempSel"
AC_MODE_PARAM        = "machMode"
AC_FAN_PARAM         = "windSpeed"
# tempIndoor / tempOutdoor are DIRECT attributes (not in settings), confirmed from diagnostics
AC_ATTR_CURRENT_TEMP     = "tempIndoor"
AC_ATTR_OUTDOOR_TEMP     = "tempOutdoor"
AC_ATTR_HUMIDITY_INDOOR  = "humidityIndoor"          # Ambient humidity (sensor reading)
AC_ATTR_HUMIDITY_SEL     = "settings.humiditySel"   # Target humidity (user setpoint)
AC_ATTR_FAN_SPEED    = "settings.windSpeed"
# Vertical swing. windDirectionVertical is an ENUM of POSITIONS, not a bool:
# 2,4,5,6,7 = fixed louver positions, 8 = SWING (oscillation). The device reports
# 0 when off: 0 is NOT among the enumValues, so sending it raises a ValueError in
# the enum setter and the API rejects it, which is the reason swing had been
# disabled. The fix (climate.py): NEVER send 0 (pre-send sanitization) and set
# windDirectionVertical only to allowed values. The real allowed values are read
# at runtime from the parameter's .values (per-device), with
# windDirectionVerticalPositionSequence as the source on the device.
AC_ATTR_SWING_V      = "settings.windDirectionVertical"
AC_ATTR_SWING_H      = "settings.windDirectionHorizontal"
AC_SWING_V_PARAM     = "windDirectionVertical"   # param name in the "settings" command
AC_SWING_H_PARAM     = "windDirectionHorizontal"
AC_SWING_V_ON        = "8"                        # 8 = vertical oscillation
AC_SWING_MODE_ON     = "on"
AC_SWING_MODE_OFF    = "off"
AC_ATTR_ON_OFF       = "settings.onOffStatus"
AC_ATTR_COMPRESSOR_FREQ = "compressorFrequency"
AC_ATTR_TOTAL_ENERGY = "totalElectricityUsed"
# Air quality (direct attributes, confirmed on Roberto's AC)
AC_ATTR_PM25        = "pm2p5ValueIndoor"   # Indoor PM2.5 (µg/m³)
AC_ATTR_CO2         = "co2ValueIndoor"     # Indoor CO2 (ppm)
AC_ATTR_CH2O        = "ch2oValueIndoor"    # Indoor formaldehyde (mg/m³)

# AC mode mapping -> HA
# Values accepted by the device: [0, 1, 2, 4, 6]
AC_MODE_MAP = {
    "0": "auto",
    "1": "cool",
    "2": "dry",
    "4": "heat",      # FIXED: "4"=HEAT confirmed from AS35PBPHRA-PRE
    "6": "fan_only",  # FIXED: "6"=FAN confirmed from AS35PBPHRA-PRE
}
AC_MODE_MAP_REVERSE = {v: k for k, v in AC_MODE_MAP.items()}

# Fan speed map (confirmed: windSpeed in settings)
# windSpeed enum (app mapFanSpeedTitle): 1=high, 2=medium(mid), 3=low, 5=auto.
# There is NO value 0 (the device rejects writing 0); auto is 5.
AC_FAN_MAP = {
    "5": "auto",
    "3": "low",
    "2": "medium",
    "1": "high",
}
AC_FAN_MAP_REVERSE = {v: k for k, v in AC_FAN_MAP.items()}

# --- Washing machine attributes -----------------------------------------------
# Confirmed from the diagnostics of the HW80-B14959TU1IT device
WM_ATTR_STATUS        = "machMode"
WM_ATTR_REMAINING     = "remainingTimeMM"
WM_ATTR_PROGRAM       = "prCode"
WM_ATTR_PROGRAM_NAME  = "programName"              # Textual program name (e.g. "Cotone")
WM_ATTR_PROGRAM_PHASE = "prPhase"                  # Cycle phase (prewash/wash/rinse/spin)
WM_ATTR_TEMP          = "temp"                     # FIXED: "tempLevel" does NOT exist on the device
WM_ATTR_SPIN_SPEED    = "spinSpeed"
WM_ATTR_TOTAL_WASH    = "totalWashCycle"
WM_ATTR_TOTAL_WATER   = "totalWaterUsed"
WM_ATTR_TOTAL_ENERGY  = "totalElectricityUsed"
WM_ATTR_CURRENT_ENERGY = "currentElectricityUsed"  # Energy of the current cycle
WM_ATTR_CURRENT_WATER  = "currentWaterUsed"         # Water of the current cycle
WM_ATTR_ON_OFF        = "onOffStatus"
WM_ATTR_DOOR          = "doorLockStatus"            # Door lock (0=unlocked, 1=locked)
WM_ATTR_DOOR_OPEN     = "doorStatus"                # Physical door (0=closed, 1=open)
WM_ATTR_ERRORS        = "errors"

# --- Tumble dryer attributes (TD) ---------------------------------------------
# The tumble dryer does NOT expose totalWashCycle; the cycle counter comes from
# programsCounter (statistics container). Confirmed on the HD100-C367GU1-IT device.
TD_ATTR_CYCLES = "programsCounter"

# --- Washing machine / tumble dryer states ------------------------------------
# Authoritative MachineMode enum from the app (decomp `MachineMode`): the washing
# group uses the same codes as MACHINE_MODE_MAP below. The previous table here was
# miscoded (e.g. 2->"paused" while 2 is EXECUTION/running, and "half_load" is a
# program option, not a machine state). Translation namespace stays "state".
WM_STATE_MAP = {
    "0": "idle",
    "1": "selection",
    "2": "running",
    "3": "paused",
    "4": "delayed_start",
    "5": "delayed_start_running",
    "6": "error",
    "7": "finished",
    "8": "test",
    "9": "stopped",
    "10": "keep_fresh",
}

# --- Additional sensors/binary for the washing group --------------------------
# Keys CONFIRMED live on Roberto's devices: washing machine HW80-B14959TU1IT and
# tumble dryer HD100-C367GU1-IT. They are direct attributes (not in settings).
WM_ATTR_DIRT_LEVEL       = "dirtyLevel"          # selected soil level (1..3)
WM_ATTR_DRY_LEVEL        = "dryLevel"            # dryness level (WD/TD)
WM_ATTR_LOADING          = "loadingPercentage"  # drum load %
WM_ATTR_DELAY            = "delayTime"           # configured start delay (minutes)
# Binary sensor (0/1). Door/door-lock already defined above: WM_ATTR_DOOR_OPEN
# (doorStatus, door open) and WM_ATTR_DOOR (doorLockStatus, door locked).
WM_ATTR_CHILD_LOCK       = "lockStatus"          # control lock (child safety)
WM_ATTR_DRUM_CLEAN       = "drumCleaning"        # recommended drum-cleaning cycle
WM_ATTR_FILTER_CLEAN     = "filterCleaning"      # recommended filter cleaning
WM_ATTR_DRY_CLEAN_NEEDED = "dryCleaningNeeded"   # recommended condenser cleaning

# Cycle phase (prPhase, raw numeric attribute). The maps translate prPhase ->
# an ENUM machine key (rendered per-language via the sensor state translations);
# washing machine/washer-dryer and tumble dryer use distinct tables. Values not
# in the map -> None (the sensor reports "unknown").
WASHING_PHASE_MAP = {
    "0": "ready",
    "1": "washing",
    "2": "washing",
    "3": "phase_skip",
    "4": "rinsing",
    "5": "rinsing",
    "6": "rinsing",
    "7": "drying",
    "8": "phase_skip",
    "9": "steam",
    "10": "ready",
    "11": "spinning",
    "12": "weighing",
    "14": "washing",
    "15": "washing",
    "16": "washing",
    "20": "rotation_start",
    "24": "refresh",
}
TUMBLE_DRYER_PHASE_MAP = {
    "0": "ready",
    "1": "heating",
    "2": "drying",
    "3": "cooling",
    "13": "cooling",
    "14": "heating",
    "15": "heating",
    "16": "cooling",
    "18": "rotation",
    "19": "drying",
    "20": "drying",
}

# --- value->machine-key maps for the Tier 2 types (read-only) -----------------
# Decodings of the hOn enums into ENUM machine keys for the sensors of the
# additional types (rendered per-language via the sensor state translations).
# Values not in the map -> None (handled by the value_fn in sensor.py).

# Authoritative app machMode (0-10), used by the types that share MachineMode
# (oven, dishwasher, ...). Kept as a SEPARATE dict from WM_STATE_MAP (currently
# identical content) so the two stay on distinct translation_keys ("machine_mode"
# vs "state") and can diverge per appliance family without affecting each other.
MACHINE_MODE_MAP = {
    "0": "idle",
    "1": "selection",
    "2": "running",
    "3": "paused",
    "4": "delayed_start",
    "5": "delayed_start_running",
    "6": "error",
    "7": "finished",
    "8": "test",
    "9": "stopped",
    "10": "keep_fresh",
}

# Dishwasher salt / rinse-aid level (saltStatus / rinseAidStatus).
DW_LEVEL_MAP = {
    "0": "ok",
    "1": "low",
    "2": "critical",
    "3": "empty",
}

# Water heater phase (prPhase -> reduced EnumWaterHeaterPhase).
WH_PHASE_MAP = {
    "0": "ready",
    "1": "heating",
    "2": "holding",
}

# Robot vacuum state (prPhase/machMode -> RVCMachModes).
RVC_STATE_MAP = {
    "0": "waiting",
    "1": "auto_cleaning",
    "2": "spot_cleaning",
    "3": "paused",
    "4": "full_and_go",
    "5": "cleaning_completed",
    "6": "charging",
}

# Robot suction power (power).
RVC_POWER_MAP = {
    "0": "auto",
    "1": "turbo",
    "2": "quiet",
}

# Washing-machine stain type (stainType -> app StainTypes enum; codes from the
# decompiled app, aligned with Andre0512/hon). 0 = none selected. Rendered
# per-language via the sensor state translations; unmapped codes -> None.
STAIN_TYPE_MAP = {
    "0": "none",
    "1": "wine",
    "2": "grass",
    "3": "soil",
    "4": "blood",
    "5": "milk",
    "6": "cooking_oil",
    "7": "tea",
    "8": "coffee",
    "9": "chocolate",
    "10": "lip_gloss",
    "11": "curry",
    "12": "milk_tea",
    "13": "chili_oil",
    "14": "blue_ink",
    "15": "color_pencil",
    "16": "shoe_cream",
    "17": "oil_pastel",
    "18": "blueberry",
    "19": "sweat",
    "20": "egg",
    "21": "ketchup",
    "22": "baby_food",
    "23": "soy_sauce",
    "24": "bean_paste",
    "25": "chili_sauce",
    "26": "fruit",
}
