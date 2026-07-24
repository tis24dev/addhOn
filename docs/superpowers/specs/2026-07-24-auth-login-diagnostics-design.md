# Auth Login Diagnostics Design

## Purpose

Issue #67 reports `ADDHON-130` because the current Salesforce login flow reaches a
HTTP 200 response that does not yield all three OAuth tokens. Existing logs identify
the failing phase but do not show enough response structure to determine which page
was received or why a particular redirect link was selected.

Add an opt-in diagnostic checkbox to the initial sign-in and reauthentication forms.
When enabled, the integration records the complete login exchange as bounded,
structured, secret-free events. A failed attempt emits those events to the normal
Home Assistant log without requiring the user to configure a DEBUG logger.

This feature observes the existing authentication protocol. It does not change link
selection, token parsing, redirects, MFA behavior, or error classification.

## User Experience

The `user` and `reauth_confirm` forms gain an optional boolean field:

- Key: `auth_diagnostics`
- Label: `Collect sign-in diagnostics`
- Default: disabled
- Description: `If sign-in fails, record sanitized technical details in the Home
  Assistant log. Credentials, tokens, and personal data are not included.`

Equivalent localized strings are added to every translation currently maintained by
the integration.

The checkbox remains selected when a failed form is shown again. It is temporary flow
state: it must be removed from submitted credentials before validation and must never
be persisted in a config entry or its options.

The existing localized error remains unchanged, for example:

```text
Could not retrieve the hOn session token. (ADDHON-130)
```

Users retrieve the evidence from Settings > System > Logs by searching for
`ADDHON-AUTH`. No logger-level action or integration options are required.

## Diagnostic Lifecycle

The config flow reads the temporary `auth_diagnostics` checkbox and passes that
boolean through `validate_input` to `HonClient`. `HonClient` creates and retains one
`AuthDiagnosticTrace` for the opted-in validation attempt. The trace receives a
random, non-sensitive eight-hex-character identifier and a monotonic sequence number.

`HonClient` owns the trace lifecycle and explicitly propagates the same instance
through:

```text
ConfigFlow -> validate_input -> HonClient(auth_diagnostics=<bool>)
  -> AuthDiagnosticTrace
  -> create_session
  -> NativeHon
  -> HonConnection
  -> HonAuth
```

The trace is owned by the validation client and follows these rules:

1. A normal sign-in failure flushes the buffered events once.
2. A successful validation discards the buffer without emitting diagnostic lines.
3. `MFAChallengeRequired` preserves the client, trace, and sequence without flushing.
4. An invalid OTP records the outcome but does not repeatedly flush the complete
   history.
5. A terminal failure after MFA flushes the complete trace once.
6. Successful MFA discards the trace after the config entry is created or updated.
7. Abandoning or removing the flow closes the client and discards the trace.
8. Flushing and discarding are idempotent.

The trace is safe across the config flow executor and the dedicated hOn loop. Its
mutable state is protected by a lock so cancellation or cleanup cannot race with event
recording or cause duplicate output.

## Components

### `AuthDiagnosticTrace`

A focused module at `client/auth_diagnostics.py` owns event buffering, validation,
serialization, and emission.

It does not expose a generic arbitrary-string logging API. Public event methods accept
only fields or structural summaries defined for that event, using integers, booleans,
`None`, or controlled enumerations:

- `request(phase, method, endpoint)`
- `response(phase, ResponseSummary)`
- `html(phase, HtmlSummary)`
- `links(phase, LinksSummary)`
- `json_shape(phase, JsonSummary)`, which emits `event=json`
- `token_shape(phase, TokenSummary)`, which emits `event=tokens`
- `page(phase, PageSummary)`, which emits `event=page`
- `skeleton(phase, HtmlSummary)`, which emits `event=skeleton`
- `verdict(phase, verdict)`, which emits `event=verdict`
- `payload(phase, kind)`
- `phase(phase, status, outcome)`

The separate `flush` and `discard` methods control terminal emission and cleanup.

Events are retained in order and serialized deterministically as single-line
`key=value` records. Lists are emitted in stable order. Every line begins with:

```text
[ADDHON-AUTH trace=7c19a2e4 seq=04]
```

The buffer is bounded to 100 events and 64 KiB of serialized event data. Once either
limit is reached, additional events are counted but not retained. The final output
reports:

```text
truncated=true dropped_events=17
```

### Structural Classifiers

Pure helper functions convert untrusted HTTP data into controlled summaries before it
reaches the trace:

- URL classifier: known endpoint or destination category only.
- Response classifier: status, elapsed time, media type, charset, byte length, and
  redirect count.
- Cookie classifier: recognized cookie-name categories and security attributes only;
  unrecognized names contribute only to an `unknown_count`.
- HTML classifier: counts and recognized structural markers.
- JSON classifier: allowed structural key names and expected container shapes.
- Token classifier: required token field presence, missing fields, duplicates,
  delimiter style, and completeness.
- Page-identity classifier: named path segments, named query parameter names, a hash of
  the path, vocabulary words present in the page title and in the visible text, the kind
  of the first form action, its method, and the redirect fingerprints of the page.
- Token-page verdict: why a page that should carry the OAuth hand-off did not.

Unknown values map to `other` or counters. They are not copied verbatim into events.

### Existing Phase Tracking

`HonAuth._phase()` remains the source of the last authentication phase and retains its
current DEBUG behavior. Diagnostic instrumentation augments the HTTP boundaries and
parsers with richer events; it does not replace error phase attribution.

The config-flow validation boundary controls the final flush because it knows whether
the complete validation, including appliance discovery, succeeded. This also permits a
failed appliance-list validation to show that authentication itself completed.

## Recorded Data

For each relevant exchange, diagnostics may record:

- sequence, semantic phase, and HTTP method;
- controlled endpoint category;
- status code and elapsed milliseconds;
- media type, charset, and body byte length;
- redirect count and categorized redirect chain;
- recognized response-cookie categories and flags, never raw names or values;
- submitted field names, never submitted values;
- HTML tag, form, input, link, and script counts;
- recognized input-name categories;
- ordered link destination categories and selected index/category;
- presence of login, privacy, ProgressiveLogin, MFA, and OAuth markers;
- JSON structural keys expected by the protocol;
- OAuth token field names present and missing;
- normal versus HTML-escaped token delimiters;
- exception class category, ADDHON error code, phase, and controlled reason;
- a structural DOM fingerprint derived only from allowlisted tag and attribute-name
  categories, with unknown names normalized and all text and attribute values excluded.

Representative failure excerpt for a post-login page whose first link leads to a CSS
asset instead of an OAuth token response:

```text
[ADDHON-AUTH trace=7c19a2e4 seq=15] event=request phase=post_login method=GET endpoint=post_login
[ADDHON-AUTH trace=7c19a2e4 seq=16] event=response phase=post_login status=200 elapsed_ms=0 media=text/html charset=utf-8 bytes=69 redirects=0 location_kind=none cookie_kinds=none unknown_cookies=0 cookie_secure=false cookie_http_only=false cookie_same_site=false
[ADDHON-AUTH trace=7c19a2e4 seq=17] event=html phase=post_login tags=2 forms=0 inputs=0 links=2 scripts=0 input_kinds=none login=true progressive_login=true otp=false privacy=false oauth_done=false page_kind=progressive_login dom=36fe5dda390e parse_error=false
[ADDHON-AUTH trace=7c19a2e4 seq=18] event=links phase=post_login count=2 kinds=static_asset,progressive_login selected_index=0 selected_kind=static_asset
[ADDHON-AUTH trace=7c19a2e4 seq=19] event=request phase=token_response method=GET endpoint=static_asset
[ADDHON-AUTH trace=7c19a2e4 seq=20] event=response phase=token_response status=200 elapsed_ms=0 media=text/css charset=none bytes=11 redirects=0 location_kind=none cookie_kinds=none unknown_cookies=0 cookie_secure=false cookie_http_only=false cookie_same_site=false
[ADDHON-AUTH trace=7c19a2e4 seq=21] event=html phase=token_response tags=0 forms=0 inputs=0 links=0 scripts=0 input_kinds=none login=false progressive_login=false otp=false privacy=false oauth_done=false page_kind=other dom=e3b0c44298fc parse_error=false
[ADDHON-AUTH trace=7c19a2e4 seq=22] event=tokens phase=token_response present=none missing=access_token,refresh_token,id_token duplicates=none html_escaped=false complete=false
[ADDHON-AUTH trace=7c19a2e4 seq=23] event=failed code=ADDHON-130 phase=get_token reason=incomplete_tokens
```

## Data That Must Never Be Recorded

The diagnostic implementation must never record:

- email addresses, passwords, or OTP values;
- access, refresh, ID, Cognito, CSRF, or remoting token values;
- authorization values;
- cookie values, Salesforce ViewState, or session identifiers;
- complete URLs, paths, query strings, fragments, or raw `href` values;
- request or response bodies;
- arbitrary page text, HTML titles, or server error text;
- arbitrary exception messages that can echo URLs or response data;
- hashes of bodies, credentials, tokens, cookies, or other secret material.

Changing a password does not reliably revoke OAuth tokens, cookies, or copied log
files, so opt-in consent is not considered a sufficient control for logging secrets.

## Logging Behavior

Events remain buffered during the attempt. On terminal failure they are emitted at
`WARNING`, followed by one terminal line containing the controlled failure reason.
This makes them visible in the default Home Assistant log without changing global or
integration logger levels.

No `ADDHON-AUTH` diagnostic lines are emitted when:

- the checkbox is disabled;
- validation succeeds;
- the flow is abandoned;
- an MFA challenge is waiting for user input.

Existing operational ERROR logs and normal DEBUG phase logs remain unchanged.

## Error Handling

Diagnostic collection must not alter the result of authentication. Classifier,
buffering, or emission failures are caught internally and reduce the diagnostic output
to a controlled `diagnostic_internal_error=true` marker. They never mask or replace
the original authentication exception.

Body reads are not duplicated. Existing response text or JSON is summarized after it
has already been read for protocol processing. Timing uses a monotonic clock.

## Testing

### Config Flow

- The checkbox appears in initial setup and reauthentication with default `false`.
- The selected value remains suggested after validation failure.
- The field is stripped before credentials are passed to validation.
- It is absent from created and updated config-entry data.
- It remains active across MFA but is not stored in `_mfa_data` credentials.

### Trace Behavior

- Disabled and successful traces emit no diagnostic lines.
- A terminal failure emits ordered lines at `WARNING` without DEBUG logging enabled.
- Flush, discard, cleanup, and truncation are deterministic and idempotent.
- The trace identifier is correctly formatted and unrelated attempts have distinct
  identifiers.
- MFA challenge, retry, terminal failure, success, and abandonment follow the lifecycle
  defined above.

### Structural Diagnostics

Fixtures cover:

- a static CSS link before the intended ProgressiveLogin link;
- a privacy-consent ProgressiveLogin page;
- an email-OTP page;
- an OAuth completion page;
- HTML-escaped token delimiters;
- missing or duplicate token fields;
- JSON, HTML, CSS, and unexpected media types;
- redirects, non-200 responses, and malformed documents.

### Leak Prevention

Malicious fixtures place unique canary secrets in credentials, OTP, URLs, headers,
cookies, HTML text, attributes, JSON values, exception messages, and every token type.
Captured logs must contain none of those canaries.

The existing AST-based auth log guard is extended to reject logging calls that pass raw
response text, bodies, URLs, headers, cookies, hrefs, credentials, OTP, or token
variables. Tests also verify that diagnostic record methods cannot accept arbitrary
extra fields.

## Acceptance Criteria

The work is complete when:

1. A non-technical user can enable diagnostics directly in setup or reauthentication.
2. A failed attempt produces a complete ordered structural trace in the normal Home
   Assistant log.
3. The trace distinguishes a token page from static content, ProgressiveLogin, privacy,
   MFA, and OAuth completion responses.
4. No secret or personal value can reach the diagnostic log through supported APIs or
   tested hostile responses.
5. The checkbox and trace are temporary and never alter persisted configuration.
6. Authentication behavior and existing error codes remain unchanged.

## Round 2: page identity and an actionable failure (issue #67)

The first release proved where a sign-in dies but not what it died on. A report showed
`page_kind=other endpoint=auth_other` for the page that should have carried the tokens,
which is not enough to answer the user, and asking for another log costs a day per
round trip. Two additions close that gap.

**Page identity.** Every HTML stop in the flow now also emits `event=page`, built from
allowlists only:

- `path_markers` and `unknown_segments`: which named path segments the landing URL has,
  and how many it has that we do not name;
- `path_hash`: a short hash of the path, so two reports of the same page are comparable
  and a working sign-in can be diffed against a broken one;
- `query_names` and `unknown_query`: named query parameter names, never their values;
- `title_markers` and `text_markers`: which words of a fixed vocabulary appear in the
  title and in the visible text. Script and style bodies are excluded, otherwise every
  word in the framework payload would look present;
- `form_action`, `form_method`, `buttons`: what the page would ask the user to submit;
- `meta_refresh`, `js_redirect`, `hon_scheme`, `oauth_done_hits`: whether the page is a
  bounce, and whether it carried the token hand-off after all.

A page the flow did not expect also emits `event=skeleton`: the bounded sequence of its
tags with their attribute names, values excluded. That is what identifies an unknown
interstitial without shipping its markup.

**Actionable failure.** `classify_token_page` turns "no tokens" into a verdict:
`password_change`, `consent`, `login`, `mfa`, `token_link_unparsed`, `empty`, or
`unknown`. The verdict is emitted as `event=verdict` and, for the two verdicts a user
can act on, the flow raises `AccountActionRequired` (ADDHON-165) with a message that
says where to go, instead of repeating the mute ADDHON-130 forever. The detection is
structural, not textual: a page carrying the OAuth hand-off never holds a password form.
`token_link_unparsed` is the mirror case and accuses our own parser rather than the
account.

The verdict runs with diagnostics off as well. A user who never ticks the checkbox still
has to be told what to do, so only the trace lines are opt-in, not the diagnosis.
