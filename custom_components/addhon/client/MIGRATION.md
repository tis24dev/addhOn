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
- [~] **Fase 3 — transport nativo (sopra il nostro auth).** Sostituire connessione
  + api HTTP, **riusando ancora il motore comandi/parametri di pyhОn** (gli si
  inietta il nostro api). Pezzi:
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
  - [x] piece 4a — FULL FLIP: `pyhon_adapter.create_session` ora ritorna `NativeHon`
    (non più `pyhon.Hon` con auth iniettato). **La produzione gira sul transport nativo**;
    `_vendor/connection/{handler,api,auth,device}` sono codice MORTO a runtime. `install_native_auth`
    non è più chiamato (legacy). Preservato il warning diagnostico "0 appliance" in `HonApi`.
    `NativeHon.close()` ora FERMA il MQTT (fix leak pyhОn). Test + LIVE-validato e2e
    (`apk/validate_flip_native_live.py`): create_session→NativeHon, 4 appliance con comandi,
    MQTT attivo, `appliance.update()` (polling) OK.
  - [ ] piece 4b — cancellare `_vendor/connection/`: BLOCCATO da due cose — (1) il `MQTTClient`
    vive lì e lo riusiamo ancora (riscriverlo nativo in `transport/mqtt.py` o rilocarlo);
    (2) `_vendor/pyhon/__init__.py` + `hon.py` importano `connection.api`/`mqtt` (rigenerati dal
    vendor script → serve cambiare `scripts/vendor_pyhon.py` per non vendorizzarli + ripulire
    `__init__`). Poi si elimina `connection/` (handler/api/auth/device-HTTP).
- [ ] **Fase 4 — motore parser nativo (= distacco TOTALE, la meta).** Riscrivere
  anche `commands`/`command_loader`/`parameter/`/`rules`/`appliance.py` con un
  modello NOSTRO (più semplice/tipizzato/idiomatico HA), validato sui dump reali,
  e cancellare l'ultimo `_vendor/pyhon/`. Era marcato "forse mai": ora è l'obiettivo
  finale dichiarato. Si fa per ultimo perché è lo strato stabile a più alto rischio
  di regressione: prima il transport (fragile), poi il motore.

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
