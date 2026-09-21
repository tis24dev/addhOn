# issue #106 — dump parziali, Haier XS 6B0S3FSB (DW)

Fonte: [issue #106](https://github.com/tis24dev/addhOn/issues/106), commento di **bbosson**
del **2026-09-21**. Non è un diagnostics completo: sono i due blocchi che servivano per
decidere la forma dei controlli DW. Il resto (schema `startProgram`, inventario entità) sta
nel corpo della issue.

Identificativi già mascherati all'origine (`***` nell'export): non c'è stato nulla da
redigere qui.

| File | Cos'è |
|---|---|
| `bbosson-XS6B0S3FSB-2026-09-21-model_attributes.json` | `data.appliances[0].model_attributes` verbatim (21 chiavi). In questo export gli apparecchi stanno sotto `data.appliances[]`, non `data.appliance`. |
| `bbosson-XS6B0S3FSB-2026-09-21-optCompatibility-decoded.json` | La chiave `optCompatibility` del file sopra, decodificata da base64. **Testo grezzo, non ri-serializzato**: contiene chiavi duplicate che qualunque parser JSON collassa, e quel dettaglio è il punto (vedi sotto). |

## Perché questi due blocchi

`optCompatibility` è la matrice che l'app hOn consulta prima di accettare una combinazione
di opzioni (`isCombinationAllowed` @ `decomp.txt:1761607`). Prima di questo dump non
compariva in nessuno dei nostri file: la sua esistenza su una DW reale era un'ipotesi.
Ora non lo è più. Analisi completa in
[`apk/analysis/issue106-dw-program-controls.md`](../../apk/analysis/issue106-dw-program-controls.md).

## La trappola del file decodificato

L'oggetto sotto la chiave `"9|12"` dichiara `opt5` due volte e `opt8` tre volte. `JSON.parse`
tiene l'ultima occorrenza, quindi l'app stessa perde tre vincoli che il cloud ha scritto.
Il file è salvato grezzo apposta: ri-serializzarlo li cancellerebbe anche qui, e non
resterebbe traccia del fatto che il dato di partenza è malformato.
