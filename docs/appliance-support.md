# Appliance support status

Coverage status per hOn appliance type in this integration.
Last updated: **2026-08-24**.

Legend:
- ✅ = implemented and active
- ◑ = partial (see notes)
- 👁 = read-only (sensors only, no commands)
- ❌ = not implemented yet

Most entities are **capability-gated**: the `binary_sensor` platform, every
control outside the AC/WM/WD/TD group, and every **read-only (👁) type** create
an entity only when the device actually declares the relevant attribute or
command in its live schema. A parameter a given model does not report is simply
not created — no permanently *unavailable* entities, and no control that writes
into nothing. The `sensor` platform of AC/WM/WD/TD is the exception: it is
defined per type and always created, so an attribute a model does not report
reads as *unavailable*.

Aliased codes are handled so a type is recognised whichever code the cloud
returns: `FR`/`FRE` map to the fridge set and `HOB` to the induction-hob set.

> **Note on the read-only (👁) types:** they are wired from the hOn parameter
> set but have **not** been validated on physical devices (none of the test
> units are of these types). Capability-gating is the safety net: only the
> parameters a device actually reports become entities. Controls (write) for
> these types are a later stage.

---

## Supported types (with control)

| Type | Code | Platforms | Read | Control (write) | Live-tested |
|---|---|---|---|---|---|
| Air conditioner | `AC` | climate, sensor, binary_sensor, switch, select | ✅ indoor/outdoor temp, humidity, PM2.5, CO₂, formaldehyde, compressor frequency, energy + filter change, CH₂O cleaning | ✅ **full**: mode (auto/cool/dry/heat/fan), target temp, fan speed, **swing**, + 16 switches (sleep, mute, eco, rapid, health, self-clean/56 °C, display, light, 10 °C heating, child lock, presence sensing, electric heating, fresh air, half-degree, energy saving) | ✅ yes |
| Washing machine | `WM` | sensor, binary_sensor, switch, select, button | ✅ state, cycle phase, program, spin speed, wash temperature, soil level, load %, delay, errors, water/energy consumption + door/door-lock/child-lock/maintenance | ◑ pause + program select/start | ✅ yes |
| Washer-dryer | `WD` | sensor, binary_sensor, switch, select, button | ✅ same as WM + dry level | ◑ pause + program select/start | ⚠️ no (no WD among test devices) |
| Tumble dryer | `TD` | sensor, binary_sensor, switch, select, button | ✅ state, cycle phase, program, dry level, load %, delay, errors, total cycles + door/child-lock | ◑ pause + program select/start | ✅ yes |
| Air purifier | `AP` | fan, sensor, binary_sensor, switch, select, number | ✅ air quality, PM2.5/PM10, VOC, CO, temperature, humidity, filter life + eco/problem | ✅ on/off + mode preset, panel light, aroma mode, child lock, touch tone; ◑ the custom-aroma timings are behind the *experimental* option | ⚠️ no (schema + field reports only) |
| Cooker hood | `HO` | fan, sensor, binary_sensor, switch, number | ✅ fan speed, errors, last work time + light, filter-cleaning alarm, filter cleaning in progress, running | ◑ power switch (the lit-or-dark control panel — while it is dark the hood ignores everything else), fan speed (0…N steps from the live schema), light switch, delayed switch-off switch and its delay in minutes. Switching the fan off stops extraction and leaves the panel and the light alone; switching the *power* off turns the light off too, which the device declares itself. **No** remote way to reset the filter alarm (the parameter is `fixed`); the clock is writable in the schema but deliberately not exposed | ⚠️ no (schema of a real HADG6DS46BWIFI, issue #83) |
| Wine cellar | `WC` | sensor, binary_sensor, switch, number | ✅ ambient + per-zone temperature and humidity, state, program name, remaining time, errors + light, presence | ◑ interior light + per-zone target temperature | ⚠️ no (schema of a real HWS77GDAU1, discussion #62) |
| Oven | `OV` | sensor, binary_sensor, number | ✅ state, cavity temperature, remaining time, delay, program name and duration, meat-probe temperatures, errors + door (main and both cavities), preheating | ◑ cavity target temperature only. Start/stop and program selection are **not** offered | ⚠️ no (no oven among test devices) |
| Induction hob | `IH` / `HOB` | sensor, binary_sensor, select | ✅ per-zone temperature, power level, pan detected, zone on, residual heat, per-zone errors and remaining time, child lock; the plate temperatures, per-zone program code/phase, flex-zone bridging and the hob timer are behind the *experimental* option | ◑ the whole-hob **power intake limit** in kW, and nothing else: the cloud schema exposes no way to switch a zone on, off or up | ⚠️ no (schema of a real HA2MTSJ68MC, issue #84) |
| Fridge / fridge-freezer | `REF` / `FR` | sensor, binary_sensor, select, number | ✅ per-zone + upper/lower + ambient temperature, ambient humidity + per-zone doors, ice maker running, ice box full, energy saving | ◑ per-zone target temperature + one program select (super cool, super freeze, holiday, the `iot_*` presets, and *off*) | ⚠️ no (the test fridge is offline; schema validated from its dump) |
| Freezer | `FRE` | sensor, binary_sensor, select, number | ✅ same set as the fridge; gating drops the unused zones | ◑ same as the fridge | ⚠️ no |

**Note on WM/WD/TD (◑):** read/monitoring is **complete**; control covers pause
and program select + start. Advanced cycle options (pre-wash, extra-rinse,
settable delay) are **intentionally deferred**: they are `startProgram` bundle
parameters and belong in the *select program → start* flow, not as standalone
switches.

**Note on the ◑ types that are not live-tested:** the controls are derived from
the LIVE command schema of a real device's diagnostics dump and are
capability-gated on it, so a model that does not declare the parameter gets no
entity. What has not been verified is the round trip on powered hardware: the
device may still refuse the command (an induction hob reporting
`remoteCtrValid = 0`, for instance).

## Read-only types (👁)

These types expose **sensors / binary sensors only**: no control has been found
for them in a device schema yet. All their entities are capability-gated and
**not** live-validated (see note above).

| Type | Code | Sensors | Binary sensors |
|---|---|---|---|
| Dishwasher | `DW` | state, program, remaining time, salt level, rinse-aid level, wash temperature, errors | door |
| Coffee machine / kettle | `KT` | instantaneous power, descaling counter, lifetime cycles | — |
| Water heater | `WH` | water / inlet / outlet temperature, power, available water volume, time-to-target, phase | indicator light, child lock |
| Robot vacuum | `RVC` | battery, state, remaining time, suction power, last/total cleaned area, errors | — |

## Not yet supported (❌)

| Type | Code | Notes |
|---|---|---|
| Microwave / toaster / blender | `MW` / `TO` / `BL` | minimal small-kitchen devices; lowest priority |

## Live-tested models

End-to-end validation on a real Home Assistant instance (shared account).

| Type | Model | Device name | Result |
|---|---|---|---|
| `AC` | **AS35PBPHRA-PRE** | "Clima camera" | ✅ climate + 16 switches + 8 sensors; swing re-enabled and validated |
| `WM` | **HW80-B14959TU1IT** | "HW80-B14959TU1IT" | ✅ 15 sensors + 6 binary + pause/program |
| `TD` | **HD100-C367GU1-IT** | "HD100-C367GU1-IT" | ✅ 9 sensors + 2 binary (door/child-lock) + pause/program |

Other known but **not** validated devices:
- A **fridge** (`REF`) on a different account, confirmed present but offline;
  never tested live.
- No real **washer-dryer** (`WD`) available: the WD code reuses the WM code
  (+ dry level) but has not been verified on a physical device.
