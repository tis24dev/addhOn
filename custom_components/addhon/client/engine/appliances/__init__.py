"""Layer per-tipo nativo (Fase 4 slice 4).

Riscrive (non copia) le appliance per-tipo di pyhОn (`_vendor/pyhon/appliances/`):
derivazioni CLIENT-SIDE (programName, modi, active/pause, available) e ritocchi alle
settings (es. dryLevel). NON vanno al cloud: l'oracolo è app + dump, non i byte.

Modellate sull'app decompilata dove è più ricca/corretta, su pyhОn dove l'app conferma,
preservando+documentando dove l'app è altitudine-sbagliata o non validabile offline.
Dettaglio e evidenze: `apk/analysis/per-type-derivations.md`. Selezione via `registry`
statico (niente import dinamico).
"""
