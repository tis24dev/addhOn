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

The config flow creates one `AuthDiagnosticTrace` for an opted-in validation attempt.
The trace receives a random, non-sensitive eight-hex-character identifier and a
monotonic sequence number.

The same trace is passed explicitly through:

```text
ConfigFlow
  -> validate_input
  -> HonClient
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

A focused module under `client/transport` owns event buffering, validation,
serialization, and emission.

It does not expose a generic arbitrary-string logging API. Public record methods accept
only fields defined for that event, using integers, booleans, `None`, or controlled
enumerations. Examples include:

- `record_request`
- `record_response`
- `record_redirects`
- `record_html_shape`
- `record_json_shape`
- `record_token_shape`
- `record_outcome`

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

Example:

```text
[ADDHON-AUTH trace=7c19a2e4 seq=01] event=request phase=introduce method=GET endpoint=authorize
[ADDHON-AUTH trace=7c19a2e4 seq=02] event=response phase=introduce status=302 elapsed_ms=184 media=text/html bytes=0 redirects=0 location_kind=login
[ADDHON-AUTH trace=7c19a2e4 seq=03] event=response phase=login_page status=200 elapsed_ms=221 media=text/html bytes=4821 redirects=1
[ADDHON-AUTH trace=7c19a2e4 seq=04] event=html phase=login_page forms=1 inputs=username,password hrefs=7 scripts=5 fwuid=true oauth_done=false
[ADDHON-AUTH trace=7c19a2e4 seq=05] event=links phase=post_login order=static_asset,progressive_login,other selected_index=0 selected_kind=static_asset
[ADDHON-AUTH trace=7c19a2e4 seq=06] event=token_response status=200 media=text/css bytes=184302 token_fields=none complete=false
[ADDHON-AUTH trace=7c19a2e4 seq=07] event=failed code=ADDHON-130 phase=get_token reason=incomplete_tokens
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
