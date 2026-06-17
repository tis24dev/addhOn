# Migrazione: dal pyhОn vendorizzato al client nativo di addhОn

Obiettivo: smettere di dipendere dal pyhОn vendorizzato (`../_vendor/pyhon/`,
non più mantenuto a monte) sostituendolo **un pezzo alla volta** con codice
nostro qui in `client/`. `_vendor/pyhon/` si svuota progressivamente fino a
sparire; nessun big-bang.

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
  `from ._vendor.pyhon import Hon`. Coupling residuo nel corpo dell'integrazione:
  la patch enum BABYCARE (`hon_client.py` ~r255, ancora import diretto di
  `_vendor.pyhon.parameter.enum`) + stringhe logger `_vendor.pyhon.*` — prossimo step.
- [ ] **Fase 2 — transport nativo.** Dietro la seam, sostituire SOLO auth +
  appliance-list + send con la nostra implementazione (flusso AWS Cognito ricavato
  dalla decompilazione dell'APK), **riusando il motore comandi/parametri di
  pyhОn**. Uccide la rottura ricorrente senza buttare i 890 LOC stabili.
  Prerequisito ideale: un device online per validare (oggi il frigo è offline).
- [ ] **Fase 3 — parser nativo (forse mai).** Sostituire anche commands/parameter/
  rules, solo se ci blocca. È stabile: bassa priorità.

## Regole di confine

1. Il codice in `client/` **non importa `_vendor.pyhon`** direttamente, tranne
   l'UNICO adattatore-ponte `pyhon_adapter.py` (già creato): è il solo file di
   `client/` con un import di `_vendor` (lazy, dentro `create_session`).
2. Il resto dell'integrazione dipende dai **Protocol** di `interfaces.py`, non
   dagli oggetti concreti di pyhОn. La sessione passa già per il ponte; l'aggancio
   diretto residuo in `hon_client.py` è la patch enum (r255), da spostare poi.
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
