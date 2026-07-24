# Auth Login Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in checkbox that emits a detailed, ordered, secret-free login trace to the normal Home Assistant log when validation fails.

**Architecture:** A new `AuthDiagnosticTrace` module owns controlled classifiers, bounded buffering, and idempotent emission. One trace is created by `HonClient`, propagated through the native session stack to `HonAuth`, populated at each existing authentication HTTP boundary, and flushed only when config-flow validation fails. Config-flow code strips the temporary checkbox before credentials are validated or persisted.

**Tech Stack:** Python 3, asyncio/aiohttp, Home Assistant config flows, voluptuous, unittest and pytest.

## Global Constraints

- Do not change login link selection, token parsing, redirects, MFA behavior, or error classification.
- Never log request or response bodies, text, complete URLs, paths, query strings, fragments, raw hrefs, values of headers/cookies/form fields, exception messages, credentials, OTPs, or any token value.
- Unknown server-controlled strings must become controlled categories or counters.
- Diagnostic failures must never mask the original authentication result.
- Emit buffered diagnostic lines at `WARNING` only after terminal failure; success, disabled diagnostics, an outstanding MFA challenge, and abandoned flows emit nothing.
- Limit one trace to 100 events and 64 KiB, report dropped events deterministically, and make flush/discard idempotent.
- Keep code and comments in English and preserve existing public-call defaults.

---

### Task 1: Secret-free diagnostic trace and classifiers

**Files:**
- Create: `custom_components/addhon/client/auth_diagnostics.py`
- Create: `tests/test_auth_diagnostics.py`

**Interfaces:**
- Produces: `AuthDiagnosticTrace(enabled: bool = False)`.
- Produces pure helpers `classify_endpoint(url)`, `summarize_response(...)`, `summarize_html(text)`, `summarize_links(hrefs, selected_index)`, `summarize_json(value)`, and `summarize_tokens(text)`.
- Produces trace methods `request`, `response`, `html`, `links`, `json_shape`, `token_shape`, `phase`, `flush`, and `discard`.

- [ ] **Step 1: Write failing classifier and lifecycle tests**

Cover controlled URL categories, HTML/JSON/token summaries, ordered output, disabled and successful silence, idempotent flush/discard, bounded truncation, and hostile canary values. Captured output must contain only the trace prefix and controlled fields.

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
python -m pytest tests/test_auth_diagnostics.py -p no:randomly
```

Expected: collection fails because `auth_diagnostics` does not exist.

- [ ] **Step 3: Implement controlled summaries**

Use frozen dataclasses for summaries. Normalize every externally supplied string through allowlists. Count unknown HTML input/link/script/cookie categories instead of copying their names or values. Compute the DOM fingerprint from allowlisted tag and attribute-name categories only.

- [ ] **Step 4: Implement bounded trace buffering**

Generate an eight-character hex trace ID with `secrets.token_hex(4)`. Protect buffer state with `threading.Lock`. Render stable `key=value` lines, enforce both limits, and make `flush(logger, code, phase, reason)` and `discard()` no-op after finalization. `flush` must catch its own errors and never raise.

- [ ] **Step 5: Verify Task 1**

Run:

```bash
python -m pytest tests/test_auth_diagnostics.py -p no:randomly
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add custom_components/addhon/client/auth_diagnostics.py tests/test_auth_diagnostics.py
git commit -m "feat(auth): add secret-free diagnostic trace"
```

---

### Task 2: Propagate one trace through the native client stack

**Files:**
- Modify: `custom_components/addhon/hon_client.py`
- Modify: `custom_components/addhon/client/factory.py`
- Modify: `custom_components/addhon/client/session.py`
- Modify: `custom_components/addhon/client/transport/connection.py`
- Modify: `custom_components/addhon/client/transport/auth.py`
- Modify: `tests/test_native_session.py`
- Modify: `tests/test_transport_connection.py`
- Modify: `tests/test_transport_auth.py`

**Interfaces:**
- `HonClient(..., auth_diagnostics: bool = False)` creates and retains the trace.
- `create_session(..., auth_trace: AuthDiagnosticTrace | None = None)`.
- `NativeHon(..., auth_trace: AuthDiagnosticTrace | None = None)`.
- `HonConnection(..., auth_trace: AuthDiagnosticTrace | None = None)`.
- `HonAuth(..., auth_trace: AuthDiagnosticTrace | None = None)`.
- `HonClient.emit_auth_diagnostics(code, phase, reason)` and `discard_auth_diagnostics()`.

- [ ] **Step 1: Write failing propagation tests**

Patch the downstream constructors and assert the exact same trace object reaches every layer. Verify all existing constructor call sites still work without the new argument.

- [ ] **Step 2: Verify propagation tests fail**

Run:

```bash
python -m pytest tests/test_native_session.py tests/test_transport_connection.py tests/test_transport_auth.py -p no:randomly
```

Expected: new keyword arguments are unsupported or the captured trace is absent.

- [ ] **Step 3: Add optional constructor parameters and client lifecycle methods**

Create the trace in `HonClient`, pass it unchanged through factory/session/connection, and inject it into `HonAuth`. On an ordinary setup exception, call the idempotent emission method after error classification. Do not emit for `MFAChallengeRequired`.

- [ ] **Step 4: Verify propagation**

Run the command from Step 2.

Expected: all selected files pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add custom_components/addhon/hon_client.py custom_components/addhon/client/factory.py custom_components/addhon/client/session.py custom_components/addhon/client/transport/connection.py custom_components/addhon/client/transport/auth.py tests/test_native_session.py tests/test_transport_connection.py tests/test_transport_auth.py
git commit -m "feat(auth): propagate login diagnostic trace"
```

---

### Task 3: Instrument the complete login exchange

**Files:**
- Modify: `custom_components/addhon/client/transport/auth.py`
- Modify: `tests/test_transport_auth.py`
- Modify: `tests/test_auth_diagnostics.py`

**Interfaces:**
- Consumes the `AuthDiagnosticTrace` API from Task 1.
- Produces ordered events for introduce, manual redirects, login page, Aura submission, post-login page, ProgressiveLogin, token response, API authorization, refresh, and MFA remoting/resume.

- [ ] **Step 1: Add failing end-to-end trace tests**

Use the existing scripted `FakeSession` and enrich `FakeResp` with response URL/history/content metadata. Assert a successful authentication populates the expected ordered in-memory events but emits nothing. Force incomplete tokens and assert the emitted dump distinguishes the selected static asset from ProgressiveLogin and reports missing token fields without containing fixture secrets.

- [ ] **Step 2: Verify the new tests fail**

Run:

```bash
python -m pytest tests/test_transport_auth.py tests/test_auth_diagnostics.py -p no:randomly
```

Expected: trace events are absent.

- [ ] **Step 3: Add request and response observations**

At every existing HTTP boundary, record a controlled request before I/O and summarize the already-read response afterward. Use `time.monotonic()` for elapsed time. Feed existing parsed text/JSON to pure summary helpers; never perform an additional response read.

- [ ] **Step 4: Add redirect, HTML, link, JSON, token, and MFA observations**

Record the complete categorized link order and selected index before following a link. Record expected/missing OAuth fields and delimiter style before raising incomplete-token errors. Preserve the same trace through MFA challenge and resume.

- [ ] **Step 5: Verify instrumentation**

Run the command from Step 2.

Expected: all tests pass and hostile canaries are absent from captured logs.

- [ ] **Step 6: Commit Task 3**

```bash
git add custom_components/addhon/client/transport/auth.py tests/test_transport_auth.py tests/test_auth_diagnostics.py
git commit -m "feat(auth): trace structured login exchange"
```

---

### Task 4: Add the temporary config-flow checkbox

**Files:**
- Modify: `custom_components/addhon/const.py`
- Modify: `custom_components/addhon/config_flow.py`
- Modify: `custom_components/addhon/translations/en.json`
- Modify: `custom_components/addhon/translations/it.json`
- Modify: `tests/test_config_flow_error_codes.py`
- Modify: `tests/test_config_flow_reauth.py`
- Modify: `tests/test_config_flow_mfa.py`
- Modify: `tests/test_translations.py`

**Interfaces:**
- Produces `CONF_AUTH_DIAGNOSTICS = "auth_diagnostics"`.
- Changes `validate_input(hass, data, *, auth_diagnostics=False)`.
- Passes credentials only to `HonClient`; the checkbox is never included in config-entry data or `_mfa_data`.

- [ ] **Step 1: Add failing config-flow tests**

Assert the checkbox is present with default false in user and reauth schemas, remains selected after errors, reaches `HonClient(auth_diagnostics=True)`, and is absent from entry creation/update data and MFA credential state.

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
python -m pytest tests/test_config_flow_error_codes.py tests/test_config_flow_reauth.py tests/test_config_flow_mfa.py tests/test_translations.py -p no:randomly
```

Expected: schema and translation assertions fail because the field is absent.

- [ ] **Step 3: Implement dynamic schemas and strip temporary input**

Build user and reauth schemas with `vol.Optional(CONF_AUTH_DIAGNOSTICS, default=<current selection>)`. Copy submitted input, pop the checkbox before validation, and create/update entries from the credential copy. Pass the boolean as the keyword-only `validate_input` argument.

- [ ] **Step 4: Complete validation lifecycle**

Instantiate `HonClient` with the flag. On appliance-discovery or wrapped validation failure, emit once with the classified code and controlled phase/reason. On complete success, discard the trace. Keep the live trace inside the MFA client and discard it after MFA success or flow removal.

- [ ] **Step 5: Add English and Italian labels/descriptions**

Add matching `data.auth_diagnostics` and `data_description.auth_diagnostics` keys under both `user` and `reauth_confirm`.

- [ ] **Step 6: Verify Task 4**

Run the command from Step 2.

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add custom_components/addhon/const.py custom_components/addhon/config_flow.py custom_components/addhon/translations/en.json custom_components/addhon/translations/it.json tests/test_config_flow_error_codes.py tests/test_config_flow_reauth.py tests/test_config_flow_mfa.py tests/test_translations.py
git commit -m "feat(config): add opt-in sign-in diagnostics"
```

---

### Task 5: Leak guards and full regression verification

**Files:**
- Modify: `tests/test_log_identity_redaction.py`
- Modify: `tests/test_auth_diagnostics.py`

**Interfaces:**
- Extends static and behavioral protection for all diagnostic call sites.

- [ ] **Step 1: Extend the AST guard**

Register `auth_diagnostics.py` and forbid raw body/text/URL/header/cookie/href/credential/OTP/token variables in logger calls. Assert `auth.py` logs only controlled diagnostic output and never passes raw values directly to a logger.

- [ ] **Step 2: Add hostile full-flow leak tests**

Place distinct canaries in email, password, OTP, all OAuth tokens, cookie values, URLs, headers, HTML text/attributes, JSON values, and exception messages. Trigger emission and assert no canary occurs in any `ADDHON-AUTH` line.

- [ ] **Step 3: Run focused security tests**

```bash
python -m pytest tests/test_auth_diagnostics.py tests/test_log_identity_redaction.py tests/test_transport_auth.py -p no:randomly
```

Expected: all pass.

- [ ] **Step 4: Run formatting and the full suite**

```bash
git diff --check
python -m pytest -p no:randomly
```

Expected: no whitespace errors and the complete suite passes.

- [ ] **Step 5: Review the final diff**

Confirm the implementation changes no authentication decisions, no temporary field is persisted, every emitted field is controlled, and only intended files changed.

- [ ] **Step 6: Commit final guards**

```bash
git add tests/test_log_identity_redaction.py tests/test_auth_diagnostics.py
git commit -m "test(auth): guard diagnostic logs against secrets"
```
