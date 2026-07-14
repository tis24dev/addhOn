# AC self-clean vs mode: compressor-off fix

Date: 2026-07-14
Status: approved design, not yet implemented
Scope: `custom_components/addhon/ac_command.py`, `custom_components/addhon/climate.py`, tests

## Problem

Roberto's AC (`climate.clima_camera`, settings-based model) ran the self-clean
cycle triggered from Home Assistant, then when it entered cool the compressor did
not engage: the indoor unit ran as a fan. A physical power cycle cleared it.

Ground truth from the appliance schema dump (`apk/dump/ac_roberto/`) and the real
`commandHistory`:

- Self-clean is a program in the Haier app: `startProgram` with
  `program = iot_self_clean` / `iot_self_clean_56`.
- The app drives this AC via `startProgram`, whose payload always carries
  `selfCleaningStatus = "0"` when starting a normal operating mode (e.g. cool with
  `machMode = "1"`).
- In the `settings` command, `settings.programRules` pins `selfCleaningStatus`
  (and `selfCleaning56Status`) to `"0"`, but only for `installationType` `1to2` /
  `1toN`. A single-split unit is `1to1`, for which the rule does not fire
  (`rules.py` `_apply_config_rules`, only `1to2`/`1toN` branches exist).

addhon side:

- `_is_program_based()` returns False for this AC because `settings` exposes
  `onOffStatus`, so the integration drives mode via the `settings` command.
- The `settings` command sends all of its parameters on every send
  (`ac_command.py` header note). A mode change writes `{onOffStatus, machMode}`
  merged into the full snapshot.
- Nothing clears `selfCleaningStatus` on a mode write, and for `1to1` the config
  rule is inert. So a cool command can ship `machMode = cool` alongside a cached
  `selfCleaningStatus = "1"`. Contradictory payload, unit runs the fan with the
  compressor off. This matches the reported symptom.

This is a latent incoherence in the settings write path, independent of whether
the specific incident was triggered by a HA command or an appliance-side
auto-transition after the clean (the latter is being confirmed separately via the
debug capture now enabled on Roberto's HA).

## Invariant

Home Assistant must never emit a `settings` command that commands active operation
while `selfCleaningStatus` / `selfCleaning56Status` is `1`. Any command that starts
or resumes a normal operating mode carries self-clean = `0`, mirroring the app's
`startProgram` payload.

## Design (surgical)

### 1. New helper in `ac_command.py`

```python
AC_SELF_CLEAN_PARAMS = ("selfCleaningStatus", "selfCleaning56Status")

def with_self_clean_off(appliance, params: dict) -> dict:
    """Return `params` plus the self-clean flags forced to '0', for the flags the
    device actually exposes on its settings command.

    The Haier app's startProgram payload always carries selfCleaningStatus=0 when
    starting a normal operating mode. On the settings write path (1to1 installs,
    where the fixed-0 programRule is inert) HA must assert the same, otherwise a
    mode command can ship machMode together with a cached selfCleaningStatus=1 and
    the unit runs the fan with the compressor off."""
    out = dict(params)
    for p in AC_SELF_CLEAN_PARAMS:
        if settings_param(appliance, p) is not None:
            out.setdefault(p, "0")
    return out
```

`setdefault` so a caller that intentionally set the flag (the self-clean switch)
is never overridden. Capability-gated by `settings_param`, so a device without the
flag gets no injected key.

### 2. Call sites in `climate.py`

Wrap the params dict at the two mode-establishing sends only:

- Active-mode branch of `async_set_hvac_mode` (currently
  `{"onOffStatus": "1", "machMode": str(mode_key)}`).
- `async_turn_on` settings-based resume (currently `{"onOffStatus": "1"}`).

Import `with_self_clean_off` from `.ac_command`.

Deliberately excluded (self-clean must NOT be cleared by these): OFF
(`onOffStatus=0`), `set_temperature`, `set_fan_mode`, `set_swing_mode`. A
temperature or fan nudge during a self-clean cycle must not abort it; only starting
or resuming a mode does.

Program-based ACs are unaffected: they already go through `startProgram`, whose
schema carries a coherent self-clean value.

## Testing

Follow the existing climate/switch test patterns.

- `set_hvac_mode(COOL)` on a device exposing both flags: the sent settings params
  include `selfCleaningStatus="0"` and `selfCleaning56Status="0"` alongside
  `onOffStatus="1"` and `machMode`.
- `set_hvac_mode` on a device WITHOUT the flags: no `KeyError`, no self-clean key
  injected.
- `async_turn_on` (settings-based): self-clean cleared.
- Regression: the `self_clean` switch turn-on still sends
  `selfCleaningStatus="1"` (helper not on that path, not clobbered).
- `set_temperature`: self-clean NOT injected (an in-progress clean is not aborted).

## Rollout

Implement on `dev`, deploy sources to pve, build there, live-validate on Roberto's
HA with debug ON: trigger self-clean, then set cool from HA, confirm the compressor
engages. Push only on explicit request.

## Caveat

`unitConfiguration` is not in the diagnostics dump (it is a device record field),
so "Roberto = 1to1" is inference from the single indoor unit, not proven. If the
debug capture shows the cool transition came from the appliance itself with no HA
command, this fix was not the trigger of that specific episode, but it still closes
the latent incoherence (mode + selfClean=1 in one settings command).
