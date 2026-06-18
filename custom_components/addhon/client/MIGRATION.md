# Migrazione: dal pyhОn vendorizzato al client nativo di addhОn

Obiettivo: smettere di dipendere dal pyhОn vendorizzato (`../_vendor/pyhon/`,
non più mantenuto a monte) sostituendolo **un pezzo alla volta** con codice
nostro qui in `client/`. `_vendor/pyhon/` si svuota progressivamente fino a
sparire; nessun big-bang.

**Stella polare (obiettivo finale, esplicito):** distacco **TOTALE** da pyhОn.
Non lo si COPIA: si scrive codice NOSTRO, più moderno, efficiente, aggiornabile
e meglio integrato con HA. La fedeltà a pyhОn vale solo dove i byte vanno al
cloud e non sono validabili offline (richieste HTTP, payload comando); ovunque
pyhОn abbia un bug o una fragilità e sappiamo fare meglio, **divergiamo e lo
documentiamo** (es. parse difensivo, timestamp comando corretto). Alla fine
sparisce anche il motore parser: la "Fase 4" NON è più "forse mai", è la meta.

## La realtà a strati (perché NON si stacca "tutto e subito")

pyhОn ≈ 3066 LOC, ma sono tre strati con profili opposti:

| Strato | LOC | Fragilità API | Riscrittura | Lì ci siamo già rotti? |
|---|---|---|---|---|
| Auth/Transport (`connection/`) | 1136 | **ALTA** | alta, dura da validare | **sì** (unified-api, token) |
| Motore comandi/parametri (`commands`, `command_loader`, `parameter/`, `rules`) | 890 | bassa | alta + alto rischio regressioni | quasi mai |
| Classi per-tipo (`appliances/*.py`) | 155 (thin) | nulla | banale | no |

Conseguenza: si stacca **per fragilità, non per facilità**. Il bersaglio vero è
il transport; il motore (stabile e complesso) si tiene il più a lungo possibile.

## Fasi (strangler pattern)

- [x] **Fase 0 — mappatura nostra.** Costruire la NOSTRA mappatura dichiarativa
  validata sui dump reali (`const.py`, `number.py`, `sensor.py`, `binary_sensor.py`).
  Sta sopra `appliance.commands`/`param`, già indipendente dagli interni di pyhОn.
  In corso (Tier 2 read-only fatto; Tier 3 controlli in corso).
- [x] **Fase 1 — seam.** `interfaces.py`: i Protocol che catturano l'esatta
  superficie stretta che usiamo. È il punto su cui far combaciare pyhОn (oggi) e
  il client nativo (domani).
- [x] **Fase 1b — adattatore-ponte sessione.** `pyhon_adapter.create_session`
  è creato: `hon_client.py` ottiene la sessione hОn da `client/`, NON più con
  `from ._vendor.pyhon import Hon`. La patch enum BABYCARE è stata spostata dietro
  il seam (`pyhon_adapter.ensure_enum_patch()`): oggi `hon_client.py` ha **ZERO
  import di `_vendor`** (solo nomi-logger stringa), blindato da una guard `ast` in
  `tests/test_session_adapter.py`. `pyhon_adapter.py` resta l'UNICO ponte verso `_vendor`.
- [x] **Fase 2 — auth nativo + FLIP.** Riscritto il flusso di login hОn (Salesforce
  OAuth) in `transport/` (`device`, `parse`, `tokens`, `headers`, `oauth`, `auth`)
  e iniettato nella macchina pyhОn (`pyhon_adapter.install_native_auth`). Il login
  di PRODUZIONE gira sul NOSTRO auth, **live-validato e2e**. Uccide lo strato dove
  ci rompevamo (unified-api, token) tenendo per ora il resto di pyhОn.
- [x] **Fase 3 — transport nativo (sopra il nostro auth) — COMPLETA.** Connessione,
  api HTTP, MQTT e orchestrazione sono NOSTRI; **`_vendor/connection/` cancellato**.
  Resta vendorizzato solo il motore comandi/parametri di pyhОn (gli si inietta il
  nostro api) = bersaglio Fase 4. Pezzi:
  - [x] piece 1 — `transport/connection.py` (`HonConnection`): get/post autenticate
    con iniezione token + retry, live-validato.
  - [x] piece 2 — `transport/api.py` (`HonApi`): i metodi HTTP (`load_*`/`send_command`)
    sopra `HonConnection`, drop-in del `HonAPI` pyhОn per il motore parser.
    Richieste byte-identiche al cloud, estrazione difensiva, differential test offline.
  - [x] piece 3 — orchestrazione `Hon` nativa: `client/session.py` (`NativeHon`),
    `__aenter__` → auth + load_appliances → costruisce gli appliance RIUSANDO
    `HonAppliance`/`HonCommandLoader` di pyhОn (via i factory `pyhon_adapter.create_appliance`/
    `create_mqtt`, così `session.py` resta _vendor-free). Espone `HonSession` + `.api`/
    `.appliances`/`subscribe_updates`/`notify` (il `MQTTClient` riusato li legge). MQTT
    gated da `enable_mqtt` (default True = pyhОn). Test offline + LIVE-validato: stessi 4
    appliance/fingerprint di pyhОn, MQTT smoke OK sopra la sessione nativa.
  - [x] piece 4a — FULL FLIP: `pyhon_adapter.create_session` ritorna `NativeHon`
    (non più `pyhon.Hon` con auth iniettato). La produzione gira sul transport nativo;
    `install_native_auth` dismesso. Warning diagnostico "0 appliance" portato in `HonApi`.
    LIVE-validato (`apk/validate_flip_native_live.py`).
  - [x] piece 4b-1 — MQTT nativo: `transport/mqtt.py` (`NativeMqttClient`, awscrt diretto),
    con un vero `stop()` (no leak). `NativeHon._make_mqtt`/`close` lo usano; rimossi i factory
    `pyhon_adapter.create_mqtt`/`stop_mqtt`. logging_utils silenzia il logger nativo. LIVE-validato.
  - [x] piece 4b-2 — **CANCELLATO `_vendor/connection/`** + `hon.py` + `__main__.py`;
    `_vendor/pyhon/__init__.py` minimale; `install_native_auth` rimosso. `scripts/vendor_pyhon.py`
    pota il transport ai rigeneri (`_prune_transport` + costante `_ENGINE_ONLY_INIT`, blindata da
    `test_vendor_script.py`). Test migrati (device→valori congelati; protocol-live→`NativeHon`).
    Verificato: motore importabile SENZA awscrt; produzione e2e LIVE OK.
- [~] **Fase 4 — motore parser nativo (= distacco TOTALE, la meta) — IN CORSO.** Riscrivere
  `commands`/`command_loader`/`parameter/`/`rules`/`appliance.py` in `client/engine/` con un
  modello NOSTRO, validato sui dump reali + sull'app decompilata, e cancellare l'ultimo
  `_vendor/pyhon/`. Piano dettagliato in `diagnostics/FASE4-engine-plan.md` (modello autorevole
  dall'app, grafo deps, vincolo isinstance, slicing). Scoperta chiave: l'app modella le rules via
  `ancillaryParameters.programRules` (pyhОn le ricostruisce diversamente, forse SBAGLIATO) ->
  al cluster l'oracolo delle rules è l'app/live-AC, non pyhОn.
  - [x] **slice 1 — parametri nativi** `client/engine/parameter/{base,fixed,enum,range}.py` + **fix
    BABYCARE** (rende `ensure_enum_patch` obsoleto). Differential test sui 67 parametri reali del
    frigo. NESSUN flip in produzione: `rules.py` usa `isinstance` contro le classi pyhОn (11 siti),
    quindi i parametri si flippano SOLO col cluster. Confutatori: HOLDS (le divergenze enum-edge
    trovate = native più corretto del patch, documentate + pinnate).
  - [x] **slice 2 — attributes nativo** `client/engine/attributes.py` (`HonAttribute`). Leaf (solo
    `client/helpers.str_to_float`). UNICA divergenza voluta = fix deprecazione: lock con
    `datetime.now(timezone.utc)` (aware) invece del deprecato `utcnow()` (naive); è interno (scritto/letto
    solo nella classe, mai mischiato con `last_update`), comportamento osservabile identico. Differential
    test sui dati shadow reali del frigo + sintetici (tests/test_engine_attributes.py). NESSUN flip
    (`appliance.py` di pyhОn costruisce ancora il SUO `HonAttribute`; il flip arriva con l'appliance, slice
    5). Confutatori: parità HOLDS, sicurezza naive->aware HOLDS; l'audit del test ha trovato buchi di
    copertura (corpus tutto stringhe-intere) -> chiusi con casi sintetici (fallback non-numerico, virgola,
    parNewVal mancante su update, non-stringa, lock in `_snap`).
  - [x] **slice 3 — cluster comandi nativo (commands+command_loader+rules+program)**
    `client/engine/{commands,command_loader,rules}.py` + `parameter/program.py` + `exceptions.py`,
    riusando i parametri dello slice 1. Differential test end-to-end sui dati reali del frigo
    (load_commands + sync) + send-path + RULES su fixture sintetiche (il frigo non ha rules, l'AC è
    offline -> oracolo sintetico, parità con pyhОn NON col modello `programRules` dell'app, rimandato
    a live-AC) + conformità Protocol. **FLIP RIMANDATO**: il pool confutatori ha scoperto che i
    parametri nativi rompono le appliance per-tipo `_extra` di pyhОn (`appliances/base.py`/`td.py`
    fanno `isinstance` contro le classi parametro di pyhОn a ogni poll: programName, dryLevel) -> quei
    siti isinstance NON erano negli "11" del ROOT, stanno nelle per-tipo = **slice 4**. Quindi cluster
    (3) e per-tipo (4) devono flippare INSIEME: `create_appliance` ritorna ancora il ROOT pyhОn; la
    sottoclasse nativa `_native_engine_appliance_cls` è pronta+differential-testata ma usata solo dai
    test. Divergenze enum-casing su favourites/recover/rule-default documentate+pinnate (eredità slice 1,
    da rivalidare live). Confutatori (4+2 round): cluster/rules parità HOLDS, flip-deferral safe, test
    rinforzati (24/26 mutanti uccisi, 2 equivalenti).
  - [x] **slice 4 — appliances per-tipo nativo + FLIP in produzione** `client/engine/appliances/`
    (`base.py` + `ref/td/wm/wd/dw/ov/wh/wc.py` + `registry.py` statico, no importlib). `create_appliance`
    ora ritorna `_native_engine_appliance_cls` (cluster comandi + per-tipo nativi iniettati nel ROOT pyhОn):
    il MOTORE in produzione è nostro; del ROOT pyhОn resta l'involucro + `HonAttribute` (slice 5). Decisioni
    app-priority (apk/analysis/per-type-derivations.md): FIX dei no-op pyhОn (modeZ1/Z2, pause, wh-active
    erano sempre falsi perché `HonAttribute` non ha `__eq__` -> confronto per valore = intento app; campi
    non consumati -> divergenza inerte+pinnata), miglioria dryLevel (nasconde anche '0', non solo '11'),
    `available` first-class; programName slug e priorità modi frigo preservati+documentati. Confutatori
    (3+1 round): flip-safety end-to-end HOLDS (integrazione, MQTT, zone>0, hon_client tutti ok con oggetti
    nativi), correttezza per-tipo HOLDS, test rinforzati (Z1/Z2 precedence + branch difensivi). Nota slice 5:
    `_vendor/printer.py` isinstance diventa cieco sui param nativi ma `appliance.diagnose` non è raggiungibile.
    290 test verdi.
  - [ ] slice 5 — appliance ROOT nativo + attributi nativi (HonAttribute) + **cancellare `_vendor/`**.

## Regole di confine

1. Il codice in `client/` **non importa `_vendor.pyhon`** direttamente, tranne
   l'UNICO adattatore-ponte `pyhon_adapter.py` (già creato): è il solo file di
   `client/` con un import di `_vendor` (lazy, dentro `create_session`).
2. Il resto dell'integrazione dipende dai **Protocol** di `interfaces.py`, non
   dagli oggetti concreti di pyhОn. La sessione e la patch enum passano entrambe
   per il ponte `pyhon_adapter.py`; `hon_client.py` è già `_vendor`-free.
   Anche `client/transport/` è `_vendor`-free (è la riscrittura nativa: non importa
   pyhОn, l'`appliance` che riceve è duck-typed).
3. `_vendor/pyhon/` è **rigenerato** da `scripts/vendor_pyhon.py` (vedi
   `_vendor/VENDOR.md`): NON si modifica a mano. Le patch finché serve pyhОn
   vivono nel fork `telard-pixel/pyhon` e si ri-vendorizzano.

## Provenienza della mappatura (vecchio vs nuovo, esplicito)

Per ogni entità/parametro tracciare la fonte, così "validato sul ferro" e "ancora
alla cieca" si distinguono a colpo d'occhio (come il flag `gated` del Tier 2):

- `dump-validated` — confermato da un dump reale (es. frigo REF HDPW5620CNPK).
- `app-mapping` — dalla mappatura decompilata §7 (ampia, non validata live).
- `pyhon-derived` — ereditato dalle classi per-tipo di pyhОn (da rivalidare).

Nota: le `appliances/*.py` di pyhОn NON sono un asset ricco (155 LOC di
pass-through). L'unico pezzo utile (`ref.py`: holidayMode/intelligenceMode/
quickModeZ1/Z2 → holiday/auto_set/super_cool/super_freeze) combacia col nostro
dump e lo ri-deriviamo noi, validato, nella mappatura nuova.
