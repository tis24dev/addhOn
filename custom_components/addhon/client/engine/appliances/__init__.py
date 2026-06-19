"""Native per-type layer.

Per-type CLIENT-SIDE derivations (programName, modes, active/pause, available) and
tweaks to the settings (e.g. dryLevel). They do NOT go to the cloud: the oracle is the
app + dumps, not the bytes.

Modeled on the decompiled app; preserved+documented where the app is altitude-wrong or
not verifiable offline.
Detail and evidence: `apk/analysis/per-type-derivations.md`. Selection via a static
`registry` (no dynamic import).
"""
