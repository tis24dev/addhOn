# Values provenance manifest

Human-readable companion to the machine-checked
[`tests/independence/provenance.json`](tests/independence/provenance.json)
(enforced by Gate B, `tests/independence/test_provenance.py`). One row per
module-level literal constant in
[`custom_components/addhon/client/transport/values.py`](custom_components/addhon/client/transport/values.py).

**Purpose.** Answer, for a licensing reviewer, the single question *"is each retained
value here because Haier requires it (interop), or because it was inherited from
another client?"* — and make the answer impossible to drift silently (CI fails if a
new `values.py` literal has no row here, or if a `must-differ` value still equals the
legacy library's).

## Classes

- **OBSERVED** — Haier-mandated. Any correct client hitting the hOn cloud must send
  this exact value/shape. Identity with another client is interop, not copying.

  **None of these is a secret.** They are PUBLIC CLIENT values: every hOn installation
  ships them, they are extractable from the app by anyone, and they identify the client
  software rather than a user or an account. `CLIENT_ID` (a Salesforce connected-app id),
  `CONFIG_API_KEY`, `AWS_ENDPOINT` and `AWS_AUTHORIZER` are all of this kind. That is why
  they live in source control instead of in the config entry: a per-user value would be
  wrong here, since the server expects the client's value, not the user's. User
  credentials and session tokens are a different matter entirely and appear nowhere in
  this file — they are supplied at runtime and never persisted in the repository.
  Treating a public client key as a confidential credential would not add security; it
  would only make the integration impossible to install.
- **CLIENT-CHOSEN** — addhOn's own identity / a format the server does not pin.
- **UNRESOLVED** — still needs an independent capture (hOn APK / mitmproxy) to be
  legitimately sourced. Kept working, but flagged so the debt stays visible.

`must-differ` = the value would betray an inherited placeholder if left equal to the
legacy library's; Gate B fails on any such row that still matches (the User-Agent row
is the one known offender, wired `xfail`).

The class fixes `must-differ`, and Gate B enforces the tie (`must_differ == class !=
OBSERVED`): **OBSERVED** rows are Haier-mandated so they MAY equal the legacy value
(`must-differ = no`); **CLIENT-CHOSEN** and **UNRESOLVED** rows are addhOn's own or
still-to-capture, so they MUST differ (`must-differ = yes`) — this stops a genuinely
inherited literal from hiding as OBSERVED to dodge the anti-copy check.

| Constant | Value | Spec | Class | must-differ | Status |
|---|---|---|---|---|---|
| `AUTH_API` | `https://account2.hon-smarthome.com` | sec2 | OBSERVED | no | ✅ sourced |
| `APP` | `hon` | sec2 | OBSERVED | no | ✅ sourced |
| `CLIENT_ID` | `3MVG9…znVO6` | sec5 | OBSERVED | no | ✅ sourced |
| `OAUTH_RESPONSE_TYPE` | `token+id_token` | sec5 | OBSERVED | no | ✅ sourced |
| `OAUTH_SCOPE` | `api openid refresh_token web` | sec5 | OBSERVED | no | ✅ sourced |
| `API_URL` | `https://api-iot.he.services` | sec2 | OBSERVED | no | ✅ sourced |
| `CONFIG_MICROSERVICE` | `config` | sec2 | OBSERVED | no | ✅ sourced |
| `CONFIG_API_KEY` | `GRCqFhC6Gk@…,MxY-configuration` | sec2 | OBSERVED | no | ✅ sourced |
| `AWS_ENDPOINT` | `a30f6tqw0oh1x0-ats.iot.eu-west-1.amazonaws.com` | sec10 | OBSERVED | no | ✅ sourced |
| `AWS_AUTHORIZER` | `candy-iot-authorizer` | sec10 | OBSERVED | no | ✅ sourced |
| `CONTENT_TYPE` | `application/json` | sec4 | OBSERVED | no | ✅ sourced |
| `OS` | `android` | sec3 | OBSERVED | no | ✅ sourced |
| `DEVICE_MODEL` | `addhon` | sec3 | CLIENT-CHOSEN | yes | ✅ differs |
| `MOBILE_ID` | `addhon` | sec3 | CLIENT-CHOSEN | yes | ✅ differs |
| `APP_VERSION` | `2.27.9` | sec3 | UNRESOLVED | yes | ⚠️ owner-action |
| `OS_VERSION` | `34` | sec3 | UNRESOLVED | yes | ⚠️ owner-action |
| `USER_AGENT` | `Chrome/999.999.999.999` | sec4 | UNRESOLVED | yes | 🔴 owner-action (xfail) |

`AUTH_HOST` is derived (`urlsplit(AUTH_API).netloc`), not a free literal, so it carries
`AUTH_API`'s provenance and is not a manifest row.

## Owner actions (the one thing re-derivation cannot fake)

These require artefacts only the repository owner can produce — a **mitmproxy/HAR
capture** of a real hOn app session and/or a dump of the hOn Android APK
(`com.haiereurope.hon`). Record the capture artefact (HAR/pcap SHA-256 or APK
path+offset) alongside the new value when you land it.

1. **`USER_AGENT`** — currently the synthetic sentinel `Chrome/999.999.999.999`, which
   is not a value any real client emits and is inherited verbatim from the legacy
   library. Capture the real device/WebView User-Agent and replace it. This is the sole
   row that still equals the legacy value, so its anti-copy check
   (`test_user_agent_is_independently_sourced`) is wired a **strict `xfail`**: the suite
   stays green while the debt stays visible. When the real UA lands, the strict xfail
   turns into an xpass that reds CI, forcing removal of the marker — the debt cannot be
   closed silently and left mislabelled as still-owed.
2. **`APP_VERSION` / `OS_VERSION`** — currently placeholders (`2.27.9` / `34`). They
   already differ from the legacy library's stale values, so Gate B passes, but they are
   still uncaptured. Confirm the real values from the APK `BuildConfig`
   (`VERSION_NAME`) and the Android API level the app presents.

## How this is enforced

- **Gate B — provenance** (`tests/independence/test_provenance.py`): every `values.py`
  literal must appear here; the manifest value must match the source; no `must-differ`
  row (except the xfail'd UA) may equal the legacy value.
- **Gate A — structure** (`tests/independence/test_structural_independence.py`): each
  transport module's structure-only AST fingerprint is scored against the legacy
  connection modules (hashes-only reference in `pyhon_fingerprints.json`; no
  third-party source is stored) by TWO **uniform** ceilings — primary **containment**
  `|ours∩pyhon|/|pyhon|` ≤ 0.50 (cannot be diluted by bolting on unrelated code) and
  secondary **Jaccard** ≤ 0.30. Six modules whose structural rewrite is deferred —
  `auth`, `api`, `mqtt`, `connection`, `oauth`, `device` (each still reproducing
  ≥ 50 % of a pyhOn module's structure) — are `xfail`. No ceiling is loosened per
  module to fake a pass.
- Spec anchors (`sec2`, `sec4`, …) point into
  [`docs/protocol/HAIER-HON-TRANSPORT.md`](docs/protocol/HAIER-HON-TRANSPORT.md).

## Inline interop literals (not yet in `values.py`)

These Salesforce/Aura and request-shape strings are **Haier/Salesforce-mandated**
(any client hitting that Lightning login / IoT command API must send them), but they
still live inline in modules that Gate A flags as pyhOn-derived, so they are disclosed
here rather than hidden. Moving them into `values.py` (and under Gate B) is part of the
deferred structural rewrite of those modules.

| Literal | Location | Class | Note |
|---|---|---|---|
| `"79;a"`, `apex://LightningLoginCustomController/ACTION$login`, `siteforce:loginApp2`, `other.LightningLoginCustom.login` | `oauth.py` `build_login_payload` (sec5) | OBSERVED | Salesforce Lightning/Aura login descriptor — the login endpoint pins these exact identifiers. |
| `transactionId=f"{mac}_{ts}"`, `attributes={channel:"mobileApp", origin:"standardProgram", energyLabel:"0"}` | `api.py` command send (sec8) | OBSERVED | hOn command envelope shape required by the IoT API. |
| login/href/fwuid regexes | `oauth.py` / `auth.py` (sec5) | OBSERVED | Behaviour-matched to Haier's login page markup; not free expression. |
| `client_id=f"{mobile_id}_{token_hex(8)}"` | `mqtt.py` MQTT connect (sec10) | OBSERVED | AWS-IoT clientId; the custom-authorizer IoT policy may pin the `<mobileId>_<hex>` separator/length, so the shape is kept for interop — only the random suffix is re-minted per connection. |
