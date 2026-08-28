# Changelog

All notable changes to addhOn, newest first. Versions follow the `vX.Y.Z` release tags.

The entries below are the published release notes for each version; the authoritative
per-release page, with the full commit list and the compare diff, is on
[GitHub Releases](https://github.com/tis24dev/addhOn/releases). Versions released before
the notes were generated automatically carry a link to their diff instead of a summary.

## [Unreleased]

**New Features**

- **A fridge's modes are no longer one dropdown** (#93). Super Cool, Super Freeze,
  Auto-set and Holiday are four separate settings on the appliance, not four values of
  one setting: the reporter pointed out that the app lets My Zone sit at 0 °C while
  Super Cool runs, and the appliance's own command catalogue agrees -- each mode writes
  exactly one register and clears nothing else. They are now four switches, one per
  mode, that can be on together. Turning one off clears only that mode, which is what
  the official app does and what the single dropdown did not: its "off" sent the reset
  that clears all four at once, so switching Super Cool off also switched Auto-set,
  Super Freeze and Holiday off.
- The **My Zone drawer's mode is now settable** on the fridges whose catalogue carries
  it -- 0 °C fresh, Quick cool, Fruit and vegetables. It has no "off" because the
  appliance has none: the drawer is always in one of its own modes.
- The fridge's **downloaded presets** (Daily use, Extra cold, Extra ice, High
  efficiency, Special food) each get a **button** that sends the preset. They are
  buttons and not switches because the appliance keeps no record of which one ran: the
  official app can only show the last one you sent from that phone.
- **Zone temperatures are set with a slider** instead of a free-text box, over exactly
  the degrees the appliance allows -- 1 to 9 for the fridge zone, -24 to -14 for the
  freezer on the reported model. The wine cooler, the oven and the cooker hood keep the
  box, and so does a zone whose temperatures the appliance publishes as a fixed list of
  choices.

**Changes**

- The single fridge **program dropdown is no longer created** where the per-mode
  controls above can be built, which is every fridge we have seen a diagnostics dump
  for. Automations calling `select.select_option` on it must move to the new switches,
  to the My Zone select, or to the preset buttons. It stays, unchanged, on a fridge
  whose catalogue offers only downloaded presets.
- The four fridge mode **readings** (`binary_sensor`) stay, and are **hidden by default
  on a new install only where a switch really replaces them** — a fridge whose appliance
  cannot clear a given mode on its own keeps that reading visible, because nothing there
  took its place. Nobody loses one either way: a reading already in your registry keeps
  working, and a hidden one is one click away in the entity settings.
- The four readings are also **renamed** — "Super cool (reading)" and so on — so the
  reading and the switch that acts on the same mode are no longer two entities with one
  name on the same device.
- The zone **setpoint controls are renamed** to say what they are ("Zone 1 target
  temperature"), which they previously shared word for word with the measured-temperature
  sensor beside them.
- **The old program dropdown is removed from the entity registry** where it was replaced,
  instead of being left behind as an unavailable entity with a "?" badge, and a **repair
  notice** tells you it happened and what replaced it. Worth reading if you automated it:
  a call to an entity that no longer exists does not fail — Home Assistant logs a warning
  and reports the service call as successful, so the automation keeps running and does
  nothing. The removal is deliberate and cannot be undone: the entity's history ends
  there. On a fridge that keeps the dropdown, nothing is touched.

**Fixes**

- **The My Zone mode sensor is removed from the registry** where the writable select
  replaced it, instead of being left behind unavailable under the same name as its
  replacement. Only there: a fridge that gets the mode switches but has no drawer
  programs keeps its sensor, because nothing took its place.
- **A four-door fridge now gets its fourth door** (discussion #94). The zone was already
  reporting its temperature; only the door was missing, on an appliance that publishes it.
- **My Zone mode no longer reports a whole-appliance preset as the drawer's state.** On
  a fridge whose downloaded presets also write the drawer's register, the mode sensor
  answered with the first preset that happened to write the same number.
- **A drawer that has modes no longer also offers a target temperature.** On the models
  that declare both, two controls wrote one register and the temperature one published
  0, 2 or 5 as degrees Celsius — "a drawer set to 0 °C fresh reads 0 °C".
- **A zone temperature can no longer be set while a mode is driving it.** Auto set, Super
  cool and Holiday each pin the fridge zone to a temperature of the appliance's choosing;
  a value sent from Home Assistant was accepted and silently overwritten. The control now
  says which mode owns the setpoint and asks you to switch it off first, which is what
  the phone app does.
- **A refused fridge command is reported in your language.** Starting a mode that the
  cloud rejected produced an untranslated "Can't send command"; switching one off, on the
  same appliance, produced a proper message. Both now answer the same way.

## [5.19.2] - 2026-08-26

**Fixes**

- **A fridge's My Zone drawer no longer reports an impossible temperature** (#75). Two
  models publish the drawer's measured temperature about 38 °C below reality while its
  setpoint stays correct -- a drawer set to -5 °C reported -43 °C -- and the other zones
  of the same appliance are exact, so nothing about the appliance as a whole was wrong.
  The reading is a real measurement and not the setpoint echoed back: with the setpoint
  changed in one step, the reading did not follow it, it walked down one degree every
  seven minutes towards its new resting point. Opening that drawer, and nothing else,
  settles which probe it is -- the reading climbs while the drawer is open and falls
  back once it is shut, which is the opposite of what a cooling coil does. Corrected, it
  rests exactly on the temperature the drawer is set to.
  The correction is applied only where a device proves it needs it, and never to the
  fridge or the freezer zone. The reading has to be impossible for the band its own
  drawer setpoint declares -- colder than any probe overshoots -- and adding the 38 °C
  back has to land it inside that band again, so a drawer that is merely very cold is
  left alone and a probe reporting nonsense is reported as it arrived rather than being
  made to look sane. A fridge that reports its drawer correctly is passed through
  untouched, and if the cloud ever fixes the reading at the source, the next Home
  Assistant start simply stops correcting it.

## [5.19.1] - 2026-08-25

**New Features**

- Cooker hoods (`HO`) gain a **power switch** (#83). The hood has three states, and the
  integration used to model only two of them: the control panel dark, the panel lit with
  the fan stopped, and the panel lit with the fan running. While the panel is dark the
  appliance ignores every speed and light command it is sent. The new switch owns the
  panel — switching it on is the remote equivalent of tapping the glass, switching it off
  stops the hood and darkens the panel — while the fan entity owns the other axis.

**Fixes**

- **Switching the hood's fan off no longer switches the whole hood off** (#83). It used
  to send the appliance's stop command, which darkened the control panel — and from the
  dark panel nothing Home Assistant sent afterwards had any effect, so the hood could be
  switched off exactly once and then had to be woken by hand at the appliance. The fan
  now writes a wind speed of zero, which is what the official app's slider does at its
  bottom notch: extraction stops, the panel stays lit, and the light keeps whatever state
  it was in.
- **The hood's speed control now wakes a sleeping hood** instead of being ignored. Speed
  changes travel on the same command the official app uses, which carries the "panel on"
  flag with them.

**Known Behaviour**

- **The hood's power switch turns the light off with it.** That is the device's own
  declaration, not a choice of this integration: its stop command pins the light to `"0"`
  as a fixed value. Stopping only the fan, with the light left alone, is what the fan
  entity now does — this note replaces the 5.18.0 one that described the old behaviour.

## [5.18.0] - 2026-08-24

**New Features**

- Cooker hoods (`HO`) gain their first controls (#83): a fan entity for the extraction
  speed, one step per level the device declares, plus switches for the light and for the
  delayed switch-off and a number for how many minutes that delay runs. Everything is
  read from the hood's live command schema, so a model that does not declare a parameter
  gets no entity for it.
- Induction hobs (`IH`/`HOB`) gain the one control the cloud lets a remote client
  change (#84): a select for the **power intake limit**, in kW. Per-zone readings arrive
  alongside it — power level, pan detection, zone on, residual heat, per-zone errors and
  remaining time — with the plate temperatures, program code and phase, flex-zone
  bridging and the hob timer behind the *experimental* option, because the only hob
  observed has never reported anything but zero for them.

**Breaking Changes**

- An induction hob used to be registered as **five** Home Assistant devices: the hob
  itself and four per-zone clones. It is now one device, which is what it is. The clone
  devices and every entity on them are **removed automatically** on upgrade; their
  entity IDs (the ones carrying `_z1` … `_z4`) disappear, and **any automation, script,
  dashboard card or template naming one of them breaks**. There is no one-to-one rename
  available: the clones duplicated readings that the surviving hob device already
  publishes under its own IDs, so an automation has to be repointed at the hob's own
  entity rather than at a renamed clone.

**Known Behaviour**

- **Turning the hood's fan off also turns its light off.** This is the device's own
  declaration, not a choice of this integration: the hood's `stopProgram` command pins
  `lightStatus` to `"0"` as a fixed value, and the official app behaves the same way.
  Switch the light back on after stopping the fan if you want it lit.
- **The hob's power limit is a cap on the WHOLE appliance, not the power of one zone.**
  It is the maximum the hob may draw, in kW, and lowering it while cooking reduces the
  power available to the zones. It cannot switch a zone on, off or up — the cloud schema
  exposes no way to do that at all.
- **A hob may refuse the command.** The hob of issue #84 reports `remoteCtrValid = 0`
  and has done since 2025-04-20, which usually means remote control is disabled on the
  appliance itself. The select is still offered — no other writable entity in this
  integration hides itself behind that flag — so a refusal surfaces as a "command
  rejected" error rather than as a missing control.

## [5.17.0] - 2026-08-24

**Diagnostics**

- The downloadable diagnostics now say WHERE the appliance-list read stopped. A dump
  reading `"appliances": []` with a null `last_error` used to be compatible with six
  different states and named none of them; the new `last_fetch` block carries the HTTP
  status, the outcome, the path segment the walk stopped on, the node type, the raw list
  length, and what setup then made of it (expanded, built, skipped by reason, kept
  degraded). `count: 0` now means one thing only: the cloud returned an empty list.
- A dump taken after a FAILED setup reports why it failed. It used to answer
  `{"status": "client_absent"}` -- the same answer given while Home Assistant is still
  retrying -- so the per-phase ledger and the appliance-list census were unreachable in
  the one file a reporter can produce for a failed setup. `last_error` now answers
  `setup_failed` with the classified code, the phase and the phase ledger.
- The record is dropped when the config entry is removed.

**Fixes**

- A census property raising while a session is torn down no longer keeps a failed setup
  from closing its client (a leaked loop thread and aiohttp session).
- Malformed appliance data and a failed appliance-list request are reported instead of
  collapsing into the same empty result.

**Privacy**

- Every value the new block emits is a token from a set written in this integration's own
  source, a range-checked integer, a shape-checked ADDHON label, or a validated instant.
  No key name and no string chosen by the cloud reaches the document.

## [5.16.0] - 2026-08-21

**New Features**

- MQTT and integration logging controls now require administrator access.
- Password fields in setup and reauthentication flows are displayed as masked inputs.
- Added complete English translations across configuration, authentication, options, services, diagnostics, entities, and error messages.

**Bug Fixes**

- Improved protection for logging controls against unauthorized access.

**Improvements**

- Updated the integration to version 5.16.0 and streamlined its runtime requirements.

## [5.15.0] - 2026-08-16

**New Features**

- Added privacy-safe poll diagnostics showing appliance counts, retained items, and categorized failures.
- Added the latest poll census to configuration diagnostics.
- Improved error reporting by distinguishing unavailable clients from healthy accounts with no appliances.
- Sensitive or unrecognized configuration values are now redacted.

**Bug Fixes**

- Prevented poll data from a previous session from appearing after a new setup.

**Tests**

- Added coverage for poll outcomes, error labeling, privacy safeguards, and diagnostic redaction.

## [5.14.1] - 2026-08-15

**Bug Fixes**

- Improved compatibility for air purifier switch entities with Home Assistant’s standard entity metadata.
- Ensured switch device-class information is handled correctly when entities are added.
- Preserved compatibility for existing legacy settings switches.

**Tests**

- Added coverage for entity registration, metadata handling, and device-class lookup.

**Chores**

- Updated the integration version to 5.14.1.

## [5.14.0] - 2026-08-15

**New Features**

- Improved support for air conditioners that use program-based power and mode controls alongside settings-based temperature and fan controls.
- Ancillary command data now includes only explicitly valued parameters, while preserving valid zero values.

**Bug Fixes**

- Corrected command handling for models exposing both program and settings controls.
- Prevented unconfigured ancillary parameters from being sent.

**Chores**

- Updated the integration version to 5.14.0.

## [5.13.0] - 2026-08-15

**Bug Fixes**

- Improved air purifier mode handling so the off state is correctly recognized without being treated as a user-selectable mode.
- Strengthened log privacy protections by consistently redacting additional identity-related fields, including nested data and varied key formatting.

**Quality Improvements**

- Added safeguards in local validation and pull request checks to prevent unwanted authorship metadata from entering project history.
- Expanded automated coverage for privacy protections and authorship-metadata checks.

## [5.12.0] - 2026-08-11

**New Features**

- Added clearer setup progress and timing diagnostics, including phase history and summaries.
- Added appliance model metadata to diagnostics.
- Appliances with temporary connection issues can be rehydrated during updates.
- Added improved retry handling for transient authentication and connection failures.

**Bug Fixes**

- Improved timeout classification and guidance for authentication and session refresh failures.
- Removed duplicated error-code prefixes from displayed messages.
- Improved handling of partially unavailable appliances and purifier controls.

**Documentation**

- Updated diagnostics documentation for model metadata.

**Release**

- Updated the component version to 5.12.0.

## [5.11.0] - 2026-08-05

**New Features**

- Diagnostics now provide detailed appliance entity inventories, including platform status and entity availability states.
- Inventory details are included in both configuration-entry and device diagnostics, with limits to prevent excessive output.

**Bug Fixes**

- Corrected air-purifier light mappings: off, low, and high now correspond to raw values 0, 1, and 2 respectively.

**Chores**

- Updated the integration version to 5.11.0.

## [5.10.0-beta1] - 2026-08-02 — pre-release

**New Features**

- Air purifier panel lights are now controlled through a select entity with Off, Low, and High options.
- Program names are translated into readable labels using localized catalogs, with graceful fallback to original values.
- Improved recovery of previously selected program categories.

**Bug Fixes**

- Carbon-monoxide status now correctly reports the cleared state.
- Errors affecting one appliance no longer prevent switches for other appliances from loading.
- Obsolete panel-light entities are cleaned up automatically.

## [5.10.0-beta] - 2026-07-30 — pre-release

Add transactional command dispatching, detailed command diagnostics, and air purifier (AP) support (fan, light, switches, aroma control, experimental entities, and diagnostics) while tightening options handling, Home Assistant stubs, and release tagging.
New Features:
- Introduce full Home Assistant support for air purifiers, including sensors, binary sensors, fan, light, switches, aroma select, timing numbers, and translations for all new entities.
- Add an experimental options toggle that gates AP experimental entities and behaviors without affecting standard functionality.
- Provide passive diagnostics for future AP capabilities by capturing unmapped command parameters, enum value deltas, and unhandled live states.
Bug Fixes:
- Fix the transactional dispatcher to canonicalize exact payloads, roll back only its own parameter mutations, and leave concurrent MQTT-driven updates intact on failure.
- Harden command diagnostics and MQTT correlation to avoid misattributing updates, handle deep or cyclic structures safely, and strictly bound logged data and identity exposure.
- Ensure options updates reapply log levels only when debug toggles change, reload entries only when experimental support changes, and preserve unknown option keys across updates.
- Correct Home Assistant test stubs to avoid clobbering shared base classes or incomplete enums that previously caused order-dependent or missing-device-class issues.
- Refine release tag handling so beta and numbered beta tags are correctly recognized and never published as stable releases.
Enhancements:
- Extend the Hon client to expose a dedicated `CommandDispatcher` and synchronous patch-dispatch helper that run patches on the internal event loop.
- Enhance engine commands and appliances with canonical exact payload handling and targeted shadow sync helpers to support transactional dispatch paths.
- Improve diagnostics output with richer per-appliance blocks, including AP-specific coverage and future capability signals, while maintaining strict redaction.
- Tighten entity translation tests to enforce option-screen parity, AP key lists per platform, capitalization/style rules, and semantic constraints on labels.
Build:
- Update the release-policy script to support numbered beta tags and drive prerelease detection with a regex-based beta suffix matcher.
Tests:
- Add extensive unit and integration-style tests for the transactional dispatcher, command diagnostics, MQTT correlation, AP entities, options flow, diagnostics coverage, translations, and stub hygiene.
- Introduce contract fixtures and tests that exercise AP intents and dispatcher behavior end-to-end against real engine command objects and snapshots.

## [5.9.3] - 2026-07-24

**New Features**

- Added clearer detection of post-login account steps, including password changes and consent pages.
- Added a dedicated error message instructing users to complete required account actions on the hOn website or app.
- Expanded optional authentication diagnostics with page identity and actionable failure details.

**Bug Fixes**

- Prevented MFA cleanup from being skipped when a configuration flow is removed.
- Improved handling of incomplete or unexpected login redirects.

**Documentation**

- Documented post-login interstitial handling and updated protocol guidance.

**Chores**

- Updated the integration to version 5.9.3.

## [5.9.2] - 2026-07-24

**New Features**

- Added an optional authentication diagnostics option to sign-in and reauthentication forms.
- When enabled, failed sign-ins can record bounded, sanitized technical details in Home Assistant logs without credentials, tokens, or personal data.
- Added support for diagnostics across redirects, token exchange, refresh, and MFA flows.

**Documentation**

- Added the HWS77GDAU1 WineCooler to the tested hardware list.
- Updated English and Italian form descriptions.

**Chores**

- Updated the integration version to 5.9.2.

## [5.9.1] - 2026-07-21

**Bug Fixes**

- Improved synchronization of appliance settings when values are missing, unknown, or not aligned with permitted increments.
- Off-grid numeric values are now adjusted to the nearest valid setting, while values outside allowed limits are clamped safely.
- Added clearer handling when a setting value cannot be applied.

**Tests**

- Added coverage for grid snapping, boundary values, and shadow-setting synchronization.

**Chores**

- Updated the integration version to 5.9.1.

## [5.9.0] - 2026-07-21

**New Features**

- Added wine-cooler interior light controls.
- Expanded settings-based switch support across compatible appliances.

**Bug Fixes**

- Improved handling of rate-limit and server errors, including retry and authentication behavior.
- Redacted appliance names in device summaries and diagnostics.

**Maintenance**

- Updated the component version to 5.9.0.
- Expanded validation and regression coverage for switches, diagnostics, and error handling.

## [5.8.3] - 2026-07-14

No release notes were published for this version. Diff: [v5.8.2...v5.8.3](https://github.com/tis24dev/addhOn/compare/v5.8.2...v5.8.3)

## [5.8.2] - 2026-07-10

No release notes were published for this version. Diff: [v5.8.1...v5.8.2](https://github.com/tis24dev/addhOn/compare/v5.8.1...v5.8.2)

## [5.8.1] - 2026-07-07

No release notes were published for this version. Diff: [v5.8.0...v5.8.1](https://github.com/tis24dev/addhOn/compare/v5.8.0...v5.8.1)

## [5.8.0] - 2026-07-06

**New Features**

- Added support for more appliance control modes, including program-based AC power/mode handling and improved program selection.
- Diagnostics now better hide sensitive device details in shared reports.

**Bug Fixes**

- Improved reliability when changing settings or starting programs, including safer rollback if a command fails.
- Fixed re-authentication and login handling so recovery is more stable.
- Prevented crashes from unexpected device data and improved availability/status accuracy.

**Chores**

- Updated the integration version.

## [5.7.2] - 2026-07-05

No release notes were published for this version. Diff: [v5.7.1...v5.7.2](https://github.com/tis24dev/addhOn/compare/v5.7.1...v5.7.2)

## [5.7.1] - 2026-07-02

**New Features**

- Expanded the list of tested hardware in the README with additional refrigerator and oven models.

**Bug Fixes**

- Improved how supported value ranges are handled, making setpoints more reliable across decimal, offset, and boundary cases.
- Prevented overly large or malformed ranges from causing excessive processing.
- Reduced drift and overshoot when generating available values.

**Chores**

- Bumped the integration version to 5.7.1.

## [5.7.0] - 2026-06-30

**New Features**

- Added new AC fan-direction controls for vertical and horizontal louver positions, including swing and position-based options.
- Expanded language support for these new select options in English and Italian.

**Bug Fixes**

- Improved how current appliance states are shown for fridge program selection, especially for edge cases and inactive modes.
- Tightened option handling so unsupported choices are not shown or sent.

## [5.6.0-beta] - 2026-06-29 — pre-release

**New Features**

- Added a new fridge/cooling program selector with live program options and an off state.
- Expanded language support for the new selector in English and Italian.
- Updated the project header with sponsor and donation badges.

**Bug Fixes**

- Improved device command handling so program changes are sent reliably and reflect the latest available options.

**Documentation**

- Added more real-hardware examples to the supported devices list.

## [5.5.0-beta] - 2026-06-28 — pre-release

**New Features**

- Added writable washer/dryer “program option” controls (select, switch, and numeric delay/option values) for supported models.
- Changes are buffered and automatically applied when you start the selected program.

**Bug Fixes**

- Improved validation and error handling for option values (prevents off-grid/invalid selections).
- Pending options are now applied reliably and only cleared after a successful start.

**Documentation**

- Updated English and Italian translations for the new program-option controls.

## [5.4.0] - 2026-06-26

**New Features**

- Added a new “Refresh now” service to trigger an immediate cloud update for all configured devices.
- Expanded documentation to highlight 2FA login support and multilingual availability.

**Bug Fixes**

- Improved device connectivity handling for more reliable online/offline status.
- Climate controls now avoid guessing unsupported heating/cooling and fan modes.
- Sensitive device details are now better redacted in logs.

**Chores**

- Updated the integration version.

## [5.3.0] - 2026-06-25

**New Features**

- Added two-factor (email OTP) sign-in and reauthentication with a dedicated code entry step, including resend support.
- Persist and reuse OAuth refresh tokens across restarts and runtime refresh/reauth token rotations to avoid full relogins.

**Bug Fixes**

- Improved MFA challenge handling so flows can resume correctly without losing the active session.
- Enhanced error classification and diagnostics with login phase and MFA status details.

**Documentation**

- Updated English and Italian UI text for 2FA steps and MFA-related messages.

**Tests**

- Added/expanded coverage for refresh-token persistence, MFA flows, diagnostics, and error routing.

## [5.2.0] - 2026-06-24

**New Features**

- Optional “minimal/validation” setup mode speeds configuration flow typing and reduces unnecessary data loading.
- AC write-path enhancements: climate entities derive modes, ranges, and capabilities from live device settings; enum-based discrete temperature setpoints are supported.

**Bug Fixes**

- Prevent fractional temperatures from being truncated; improve HVAC/fan/swing command payload handling and rollback on failed sends.
- Sensor fallback now treats specific “zero/empty” values as placeholders.
- Legacy “power” entity cleanup is now limited to the correct entity type.

**Improvements**

- MQTT realtime updates are more robust; config flow now shows structured, code-based connection/auth errors and diagnostics include last error details.
- Stronger privacy by consistently redacting sensitive identifiers in logs.

**Tests**

- Expanded coverage for MQTT, config flow errors, redaction, and AC write paths.

## [5.1.0] - 2026-06-22

No release notes were published for this version. Diff: [v5.0.7...v5.1.0](https://github.com/tis24dev/addhOn/compare/v5.0.7...v5.1.0)

## [5.0.7] - 2026-06-22

No release notes were published for this version. Diff: [v5.0.6...v5.0.7](https://github.com/tis24dev/addhOn/compare/v5.0.6...v5.0.7)

## [5.0.6] - 2026-06-21

No release notes were published for this version. Diff: [v5.0.5...v5.0.6](https://github.com/tis24dev/addhOn/compare/v5.0.5...v5.0.6)

## [5.0.5] - 2026-06-21

No release notes were published for this version. Diff: [v5.0.4...v5.0.5](https://github.com/tis24dev/addhOn/compare/v5.0.4...v5.0.5)

## [5.0.4] - 2026-06-20

No release notes were published for this version. Diff: [v5.0.2...v5.0.4](https://github.com/tis24dev/addhOn/compare/v5.0.2...v5.0.4)

## [5.0.2] - 2026-06-19

No release notes were published for this version. Diff: [v5.0.1...v5.0.2](https://github.com/tis24dev/addhOn/compare/v5.0.1...v5.0.2)

## [5.0.1] - 2026-06-19

No release notes were published for this version. Diff: [v5.0.0...v5.0.1](https://github.com/tis24dev/addhOn/compare/v5.0.0...v5.0.1)

## [5.0.0] - 2026-06-19

No release notes were published for this version. Diff: [v4.1.0...v5.0.0](https://github.com/tis24dev/addhOn/compare/v4.1.0...v5.0.0)

## [4.1.0] - 2026-06-19

No release notes were published for this version. Diff: [v4.0.0...v4.1.0](https://github.com/tis24dev/addhOn/compare/v4.0.0...v4.1.0)

## [4.0.0] - 2026-06-19

No release notes were published for this version. Diff: [v3.0.0...v4.0.0](https://github.com/tis24dev/addhOn/compare/v3.0.0...v4.0.0)

## [3.0.0] - 2026-06-17

No release notes were published for this version. Diff: [v2.7.1...v3.0.0](https://github.com/tis24dev/addhOn/compare/v2.7.1...v3.0.0)

## [2.7.1] - 2026-06-16

No release notes were published for this version. Diff: [v2.7.0...v2.7.1](https://github.com/tis24dev/addhOn/compare/v2.7.0...v2.7.1)

## [2.7.0] - 2026-06-16

No release notes were published for this version. Diff: [v2.6.2...v2.7.0](https://github.com/tis24dev/addhOn/compare/v2.6.2...v2.7.0)

## [2.6.2] - 2026-06-16 — pre-release

No release notes were published for this version. Diff: [v2.6.1...v2.6.2](https://github.com/tis24dev/addhOn/compare/v2.6.1...v2.6.2)

## [2.6.1] - 2026-06-15

No release notes were published for this version. Diff: [v2.6.0...v2.6.1](https://github.com/tis24dev/addhOn/compare/v2.6.0...v2.6.1)

## [2.6.0] - 2026-06-14

No release notes were published for this version. Diff: [v2.5.0...v2.6.0](https://github.com/tis24dev/addhOn/compare/v2.5.0...v2.6.0)

## [2.5.0] - 2026-06-14

No release notes were published for this version. Diff: [v2.4.6...v2.5.0](https://github.com/tis24dev/addhOn/compare/v2.4.6...v2.5.0)

## [2.4.6] - 2026-06-13

No release notes were published for this version. Diff: [v2.4.5...v2.4.6](https://github.com/tis24dev/addhOn/compare/v2.4.5...v2.4.6)

## [2.4.5] - 2026-06-13

No release notes were published for this version. Diff: [v2.4.4...v2.4.5](https://github.com/tis24dev/addhOn/compare/v2.4.4...v2.4.5)

## [2.4.4] - 2026-06-13

No release notes were published for this version. Diff: [v2.4.3...v2.4.4](https://github.com/tis24dev/addhOn/compare/v2.4.3...v2.4.4)

## [2.4.3] - 2026-06-13

No release notes were published for this version. Diff: [v2.4.2...v2.4.3](https://github.com/tis24dev/addhOn/compare/v2.4.2...v2.4.3)

## [2.4.2] - 2026-06-13

No release notes were published for this version. Diff: [v2.4.0...v2.4.2](https://github.com/tis24dev/addhOn/compare/v2.4.0...v2.4.2)

## [2.4.0] - 2026-06-13

No release notes were published for this version. Diff: [initial...v2.4.0](https://github.com/tis24dev/addhOn/releases/tag/v2.4.0)

---

## Pre-release history

The section below is the changelog as it was written for v2.0.3, before this project
adopted the current release process. It is kept verbatim, in the language it was written
in, as a historical record.

### v2.0.3 — original notes

#### 🔴 CRITICAL FIXES

##### hon_client.py
- **Rimosso duplicato `run_command_sync()`** (riga ~175-180)
  - La funzione era definita due volte, la seconda sovrascriveva la prima
  - Mantenuta una sola definizione chiara e funzionante

---

#### 🟠 MAJOR FIXES

##### climate.py
- **Semplificato HVACMode enum handling** (righe 83-94)
  - ❌ `mode_str = hvac_mode.value if hasattr(hvac_mode, "value") else str(hvac_mode)`
  - ✅ `mode_str = hvac_mode.value` (HVACMode è StrEnum, .value basta)
  - Rimosso `.lower()` ridondante

- **Usate costanti da const.py per coerenza** (righe 42, 55)
  - ❌ `self._get_attr("machMode", "1")` → ✅ `self._get_attr(AC_ATTR_MODE, "1")`
  - ❌ `self._get_attr("tempSel")` → ✅ `self._get_attr(AC_ATTR_TEMP)`
  - ❌ `self._get_attr("onOffStatus", "0")` → ✅ `self._get_attr(AC_ATTR_ON_OFF, "0")`
  - ❌ `self._get_attr("windSpeed", "0")` → ✅ `self._get_attr(AC_ATTR_FAN_SPEED, "0")`
  - Aggiunto import di tutte le costanti necessarie

- **Fixed fallback target_temperature**
  - ❌ `return 24.0 if val is None` (menzogna al frontend)
  - ✅ `return None` (dato non disponibile)
  - Ora coerente con `current_temperature` che ritorna None

---

#### 🟡 MINOR FIXES

##### select.py
- **Fixed falsy check su programma 0** (righe 65-71)
  - ❌ `code = (self._get_attr(...) or self._get_attr(...) or ...)` 
    - Scarta il valore 0 come falsy
  - ✅ Loop esplicito con check `is not None`
    - Accetta correttamente il programma 0 (Cotone)

##### manifest.json
- **Aggiornato numero versione** da 2.0.2.1 → 2.0.3 (release delle fix)

---

#### 📋 SUMMARY

| File | Tipo Fix | Numero Cambi |
|------|----------|-------------|
| hon_client.py | CRITICAL | 1 |
| climate.py | MAJOR | 4 |
| select.py | MINOR | 1 |
| manifest.json | VERSION | 1 |

**Totale:** 8 fix applicati

---

#### 🧪 Testing Recommendation

Dopo il deploy, verificare:
1. ✅ Climate entity: cambio modalità (OFF/COOL/HEAT/etc.)
2. ✅ Climate entity: lettura temperatura corretta (non 24.0 di default)
3. ✅ Select entity: selezione programma 0 (Cotone)
4. ✅ Sensori: umidità interna registrata correttamente

---

#### 📝 Note Tecniche

- **HVACMode**: Home Assistant usa StrEnum, quindi `.value` torna direttamente la stringa ("cool", "heat", ecc.)
- **Fallback temperature**: Ritornare un valore fittizio è peggio che ritornare None — il frontend vede None come "dato in caricamento"
- **Falsy check in Python**: `or` scarta 0, "", False — usare `is not None` per valori che possono essere 0

---

#### 🔗 Git Info

- **Versione precedente:** 2.0.2.1 (con bug)
- **Versione corrente:** 2.0.3 (fix applicati)
- **Branch:** main
- **Compatibility:** pyhOn >= 0.17.5

