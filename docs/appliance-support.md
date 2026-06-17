# Appliance support status

Coverage status per hOn appliance type in this integration.
Last updated: **2026-06-17**.

Legend:
- ✅ = implemented and active
- ◑ = partial (see notes)
- 👁 = read-only (sensors only, no commands)
- ❌ = not implemented yet

Some platforms are **capability-gated**: the `binary_sensor` platform, the AC
switches, and every **read-only (👁) type** create an entity only when the
device actually exposes the relevant attribute/command. So on the read-only
types a parameter a given model does not report is simply not created (no
permanently *unavailable* entities). The control-capable types' `sensor`
platform (AC/WM/WD/TD) is instead defined per type and always created; an
attribute a model does not report there reads as *unavailable*.

> **Note on the read-only (👁) types:** they are wired from the hOn parameter
> set but have **not** been validated on physical devices (none of the test
> units are of these types). Capability-gating is the safety net: only the
> parameters a device actually reports become entities. Controls (write) for
> these types are a later stage.

---

## Supported types (with control)

| Type | Code | Platforms | Read | Control (write) | Live-tested |
|---|---|---|---|---|---|
| Air conditioner | `AC` | climate, sensor, switch | ✅ indoor/outdoor temp, humidity, PM2.5, CO₂, formaldehyde, compressor frequency, energy | ✅ **full**: mode (auto/cool/dry/heat/fan), target temp, fan speed, **swing**, + 16 switches (sleep, mute, eco, rapid, health, self-clean/56 °C, display, light, 10 °C heating, child lock, presence sensing, electric heating, fresh air, half-degree, energy saving) | ✅ yes |
| Washing machine | `WM` | sensor, binary_sensor, switch, select, button | ✅ state, cycle phase, program, spin speed, wash temperature, soil level, load %, delay, errors, water/energy consumption + door/door-lock/child-lock/maintenance | ◑ pause + program select/start | ✅ yes |
| Washer-dryer | `WD` | sensor, binary_sensor, switch, select, button | ✅ same as WM + dry level | ◑ pause + program select/start | ⚠️ no (no WD among test devices) |
| Tumble dryer | `TD` | sensor, binary_sensor, switch, select, button | ✅ state, cycle phase, program, dry level, load %, delay, errors, total cycles + door/child-lock | ◑ pause + program select/start | ✅ yes |

**Note on WM/WD/TD (◑):** read/monitoring is **complete**; control covers pause
and program select + start. Advanced cycle options (pre-wash, extra-rinse,
settable delay) are **intentionally deferred**: they are `startProgram` bundle
parameters and belong in the *select program → start* flow, not as standalone
switches.

## Read-only types (👁)

These types expose **sensors / binary sensors only** (no controls yet). All
their entities are capability-gated and **not** live-validated (see note above).
Aliased codes (`FR`/`FRE` → fridge set, `HOB` → hob set) are handled so the type
is recognised whichever code the cloud reports.

| Type | Code | Sensors | Binary sensors |
|---|---|---|---|
| Fridge / fridge-freezer | `REF` / `FR` | per-zone + upper/lower + ambient temperature, ambient humidity | per-zone doors, ice maker running, ice box full, energy saving |
| Freezer | `FRE` | (same set as fridge; gating drops the unused zones) | (same as fridge) |
| Oven | `OV` | state, cavity temperature, remaining time, meat-probe temperatures | door (main + per cavity) |
| Dishwasher | `DW` | state, program, remaining time, salt level, rinse-aid level, wash temperature, errors | door |
| Wine cellar | `WC` | ambient + zone temperature, remaining time | interior light, presence |
| Hob / cooktop | `IH` / `HOB` | per-zone temperature (up to 5 zones) | pan detected per zone (up to 6) |
| Hood | `HO` | fan speed | light, filter-cleaning alarm |
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
