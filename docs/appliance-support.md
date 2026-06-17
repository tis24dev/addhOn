# Appliance support status

Coverage status per hOn appliance type in this integration.
Last updated: **2026-06-17**.

Legend:
- ✅ = implemented and active
- ◑ = partial (see notes)
- 👁 = read-only (no commands)
- ❌ = not implemented yet (parameters already mapped, see below)

Some platforms are **capability-gated**: the `binary_sensor` platform and the AC
switches create an entity only when the device actually exposes the relevant
attribute/command. The other platforms (notably `sensor`) are defined per
appliance type; an attribute a given model does not report simply reads as
*unavailable* rather than suppressing the entity.

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

Currently **none**: every implemented type has at least one control. Read-only
will be the initial stage for the types below when they are added (Tier 2:
sensors first, commands later).

## Not yet supported (❌)

These types are not exposed as entities yet. The required parameters are
identified and support is planned (Tier 2: read-only sensors first, then
controls).

| Type | Code | Planned HA platform | Notes |
|---|---|---|---|
| Fridge / fridge-freezer | `REF` / `FR` | climate (per zone), sensor, binary_sensor, switch, select | multi-zone, ice maker, "My Zone" |
| Freezer | `FRE` | sensor, switch, climate | subset of the fridge |
| Oven | `OV` | sensor, binary_sensor, number, select, button | meat probes, phases, programs/recipes |
| Dishwasher | `DW` | sensor, binary_sensor, switch, select | salt/rinse-aid, options |
| Wine cellar | `WC` | climate (per zone), sensor, switch | light, presence sensor |
| Hob / cooktop | `IH` / `HOB` | sensor, binary_sensor, number | up to 6 zones |
| Hood | `HO` | fan/sensor, switch, select | speed, light, filters |
| Coffee machine / kettle | `KT` | sensor, select, button | recipes, descaling |
| Water heater | `WH` | water_heater, sensor, switch | eco/boost/anti-legionella modes |
| Robot vacuum | `RVC` | **vacuum**, sensor, select, button | battery, power, modes (map = cloud-only) |
| Microwave / toaster / blender | `MW` / `TO` / `BL` | sensor, select, button | small kitchen |

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
