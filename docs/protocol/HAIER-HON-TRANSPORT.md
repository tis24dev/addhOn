# Haier hOn transport protocol — independent contract (HHT)

Anchor prefix: **HHT-secN**. The addhOn transport modules cite these anchors in
their docstrings/comments so a reviewer can walk *spec → code* without ever
consulting a third-party library.

## sec0 — Authorship & provenance

This document is an **independent** description of the wire protocol addhOn's
transport speaks to the Haier hOn cloud. It was authored from:

- **Public standards** — RFC 6749 (OAuth 2.0, esp. sec4.2 implicit flow), RFC 7519
  (JSON Web Token, esp. sec4.1.4 `exp`), RFC 4122 (UUID), the OpenID Connect `nonce`
  requirement.
- **Public vendor documentation** — Salesforce Lightning/Aura login-form POST shape
  and JS-Remoting (`@RemoteAction`) protocol; AWS IoT Core MQTT-over-WebSocket
  **custom authorizer** contract and the `aws-iot-device-sdk`/`awscrt` builder API.
- **Observed behaviour** of the hOn cloud (endpoint hosts, header names, response
  shapes, status semantics).

No clause here is transcribed from any GPL/MIT third-party client. Where a value
coincides with another client's, it is because **Haier dictates it** (interop); such
values are classified `OBSERVED` in the provenance manifest and are legitimately
identical. Values a client is genuinely free to choose (User-Agent, the OAuth
`nonce`, token-lifetime heuristic, client identity strings) are **re-derived** here
and MUST NOT be inherited — see `VALUES-PROVENANCE.md` and `tests/independence/`. The
MQTT `clientId` *format* is a deliberate exception: AWS-IoT needs only uniqueness, but
the custom-authorizer IoT policy may pin the separator/length, so its
`<mobileId>_<hex>` shape is kept as interop (`OBSERVED`) and only its random suffix is
re-minted — see sec10.

## sec1 — Classification rule

Every retained value is one of:

- **OBSERVED / Haier-mandated** — any correct client hitting the hOn cloud must send
  this exact value or shape (host, path, header name, payload key, AWS authorizer
  name). Identity with another client is *interop*, not copying.
- **CLIENT-CHOSEN** — addhOn's own identity or a format the server does not pin
  (e.g. `deviceModel`/`mobileId`, the UUID shape of the nonce).
- **UNRESOLVED** — a value that still needs an independent capture (hOn APK /
  mitmproxy) to be legitimately sourced (User-Agent, real `appVersion`/`osVersion`).

The machine-checkable register is `tests/independence/provenance.json` (Gate B).

## sec2 — Endpoints & hosts

| Surface | Host | Auth class |
|---|---|---|
| OAuth / login (Salesforce-fronted) | `https://account2.hon-smarthome.com` (`AUTH_API`) | public → tokens |
| IoT command API | `https://api-iot.he.services` (`API_URL`) | `cognito-token` + `id-token` |
| MQTT realtime (AWS IoT ATS) | `a30f6tqw0oh1x0-ats.iot.eu-west-1.amazonaws.com` (`AWS_ENDPOINT`) | custom authorizer |

Custom redirect scheme: `hon://mobilesdk/detect/oauth/done` (`APP = "hon"`).

## sec3 — Client identity descriptor

Every request carries a device identity object. Keys (server reads them verbatim):

```
{ "appVersion": <str>, "mobileId": <str>, "os": "android",
  "osVersion": <int>, "deviceModel": <str> }
```

On the *mobile* variant of a call, the `os` key is renamed to `mobileOs` (same value)
— `{ …, "mobileOs": "android" }`, no `os` key. The official app fills these from the
running device; addhOn runs **headless**, so it sends a fixed identity
(`deviceModel = mobileId = "addhon"`) that presents as addhOn. `appVersion`/`osVersion`
track the current app but are **UNRESOLVED** placeholders until captured from the APK.

## sec4 — HTTP header contract

**4.1 Base headers (every request).**
- `Content-Type: application/json` — the JSON command endpoints reject form bodies
  (Haier-mandated; standard media type, not a client invention).
- `user-agent: <UA>` — see 4.3.

**4.2 Authenticated headers.** Two tokens injected on every authenticated call, keyed
exactly as the server reads them: `cognito-token: <cognitoUser.Token>` and
`id-token: <id_token>`. Merge order: caller `extra` overrides the base headers; the
two tokens override everything (never caller-spoofable).

**4.3 User-Agent policy (CLIENT-CHOSEN / UNRESOLVED).** The hOn cloud has historically
accepted an arbitrary UA. `Chrome/999.999.999.999` is a **synthetic sentinel** — no
real browser or device emits it, so reproducing it is only possible by inheriting a
placeholder. It is therefore flagged `must_differ_from_pyhon` and is an **OWNER-ACTION**
capture target (real device/WebView UA from the APK or a mitmproxy session). Until
captured, the sentinel is kept only to preserve wire behaviour and the provenance gate
marks the row expected-red.

## sec5 — OAuth / login handshake

Implicit flow (RFC 6749 sec4.2). Sequence: **authorize → login-page scrape → login
POST (Aura) → token redirect → API login (`/auth/v1/login`)**.

**5.1 Authorize URL** (`GET {AUTH_API}/services/oauth2/authorize/expid_Login?…`).
Query is built **by hand** (NOT urlencoded) because the server is strict:
`response_type=token+id_token` (literal `+`), `client_id=<CLIENT_ID>`,
`redirect_uri=<quote("hon://mobilesdk/detect/oauth/done")>` (only `:`→`%3A`, slashes
kept), `display=touch`, `scope=api openid refresh_token web` (spaces kept), `nonce=<n>`.

**5.2 nonce.** OpenID Connect requires *a* fresh nonce; the **format is a free client
choice**. addhOn mints an **RFC 4122 v4 UUID** (`str(uuid.uuid4())`) — an authored
choice, not a hand-sliced hex string.

**5.3 Login-page scrape.** The authorize response bootstraps navigation via a
`url = '…'` / `href = '…'` sink in inline markup/JS; addhOn extracts the FIRST
single-quoted target. A post-Jul-2024 relative `/NewhOnLogin…` target is rewritten
back onto the old `{AUTH_API}/s/login…` endpoint. If the response is already the token
redirect (`oauth/done#access_token=…`), no login is needed.

**5.4 Login POST** (`POST {AUTH_API}/s/sfsites/aura`). Salesforce Lightning Aura
contract, body `&`-joined as `f"{k}={quote(json.dumps(v))}"` over an ordered dict:
`message` (`actions[0]` = the `LightningLoginCustomController/ACTION$login` descriptor
with `username`/`password`/`startUrl`), `aura.context`
(`mode=PROD`, `fwuid`, `app=siteforce:loginApp2`, `loaded`, `dn=[]`, `globals={}`,
`uad=False`), `aura.pageURI`, `aura.token=None`; params `{r:3,
other.LightningLoginCustom.login:1}`. Key order and encoding are Aura-mandated.

## sec6 — Token model & lifetimes

**6.1 Inventory.**

| Token | Origin | Sent as | Encoding |
|---|---|---|---|
| `access_token` | OAuth redirect fragment | refresh input | raw (not URL-decoded) |
| `refresh_token` | OAuth redirect fragment | refresh POST body | URL-decoded once (`unquote`) |
| `id_token` | OAuth redirect fragment | `id-token` header + MQTT `auth_token_value` | JWT |
| `cognito-token` | `POST /auth/v1/login` → `cognitoUser.Token` | `cognito-token` header | — |

**6.2 Redirect-fragment parsing** (RFC 6749 sec4.2.2). Tokens return in the URL
**fragment** as `&`-delimited `name=value` fields. Contract:
- a field value runs to the next `&` **or the end of the fragment** — a trailing field
  needs **no** closing `&`;
- the field name is anchored to a delimiter boundary (`start | # | ? | &`) so
  `access_token` cannot match inside a longer key;
- `access_token`/`id_token` are handed to the cloud **raw** (not percent-decoded);
  only `refresh_token` is `unquote`d once;
- a field is "present" if its key appears, even with an empty value; the redirect is
  *complete* iff all three keys are present.

**6.3 Expiry derivation (CLIENT-CHOSEN — re-derived).** A hardcoded "expires after N
hours" heuristic is **not server-stated**. addhOn reads the JWT `exp` claim of the
returned token (RFC 7519 sec4.1.4): decode the unverified payload (the IdP already
signed it; we only need the stated lifetime so we never send a token past its own
`exp`), treat it as expired at `exp` and "expiring soon" at `exp − skew`. When `exp`
is unreadable, fall back to a **conservative** documented window — never a long
invented lifetime. This makes the lifetime traceable to the token itself.

## sec7 — 2FA email-OTP sub-protocol

When email 2FA is enabled, the post-login `/apex/ProgressiveLogin` page is the OTP
step. It is driven by Salesforce **JS-Remoting** (`@RemoteAction` on
`ProgressiveLoginController`: `verifyEmailOTP` / `resendEmailCode`) via
`POST /apexremote`, finished by a VisualForce form postback (ViewState hidden inputs +
the `jsfcljs` commandLink). This is a standard Salesforce web mechanism (the native
app uses AWS Cognito instead), documented here to keep it spec-traceable.

## sec8 — Command API contract

`POST /commands/v1/*` (send) and the `load_*` reads on `API_URL`. Success gate:
`payload.resultCode == "0"`; any other value / missing key → treat as failure and
return the safe empty default (never crash on a malformed body). `send_command`
carries `attributes` (`channel`, `origin`, `energyLabel`), a `transactionId` of
`f"{mac}_{ts}"`, and the program name upper-cased.

**Timestamp.** ISO-8601 **UTC** with exactly **3-digit milliseconds** and a `Z` suffix
(`2026-06-18T12:34:56.789Z`). Milliseconds are always 3 digits, including `.000` — the
seconds are never lost. Milliseconds **truncate**, they do not round.

## sec9 — Unified-api appliance-list contract

`POST /unified-api/v1/view/appliance-list` (returns offline devices too). Extract the
list at `modules.applianceList.payload.appliances`. **Fail-safe:** any unexpected shape
— a missing key, a non-dict intermediate level, or a non-list final value — yields `[]`
(schema drift is "0 appliances", never a crash). A truthy non-list final value also
logs a warning.

## sec10 — MQTT / AWS-IoT custom-authorizer contract

MQTT-over-WebSocket to `AWS_ENDPOINT` via the AWS IoT **custom authorizer**
`candy-iot-authorizer` (`AWS_AUTHORIZER`). The builder kwargs are dictated by the
`awscrt`/`aws-iot-device-sdk` custom-authorizer API + Haier's authorizer config:
`auth_authorizer_name`, `auth_authorizer_signature` (the introspection token),
`auth_token_key_name = "token"`, `auth_token_value = <id_token>`, `client_id`. The MQTT
**clientId** is `<mobileId>_<16 hex>` — a fresh `secrets.token_hex(8)` suffix minted
per connection. AWS-IoT only requires the clientId to be *unique*, but the shape is
treated as `OBSERVED`/interop and kept byte-compatible with the historical client: the
custom-authorizer IoT policy may pin the separator/length, so only the random suffix is
regenerated, not the format (`mqtt.py`). Lifecycle callback method *names* are addhOn's
own.

## sec11 — Error & retry semantics

Status routing on the command surface: `401`/`403` with a JSON body → refresh → reauth
ladder; `403` with an HTML body → transient (WAF/edge); `429` → rate-limited; `5xx` →
transient; a non-JSON body where JSON is expected → decode error. The retry machinery
(single-flight generation counter, per-request timeouts, backoff) is addhOn-original.

## sec12 — Security invariants

- **SSRF host-pinning.** Login-flow hrefs never legitimately leave `AUTH_API`. Every
  scraped href is resolved and, if the resolved result is off-host on a scheme aiohttp
  would actually connect (`http`/`https`/`ws`/`wss`/`tcp`), it is **re-pinned** to
  `AUTH_API` with the foreign authority demoted to a path segment. The check is on the
  *resolved* host, so whitespace/control-char, protocol-relative (`//host`), and
  empty-authority (`http:///host`) bypasses cannot slip the token fetch off-host.
- **Redaction.** Identity/secrets (tokens, MAC, serial, email) are redacted from logs.
