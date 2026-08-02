# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Haier-cloud interop values -- the provenance-tracked home for the transport's
module-level endpoint and identity literals.

Scope note (do not overclaim): this is NOT yet the home of *every* literal the
transport sends. Some Salesforce/Aura login descriptors, the command request-shape
strings and the `transactionId` format still live inline in oauth.py/api.py/auth.py.
Those modules are flagged as still-pyhOn-derived by the structural gate (Gate A,
xfail), and the inline literals are catalogued in VALUES-PROVENANCE.md -- so they are
disclosed, not hidden. Moving them here is part of the deferred structural rewrite.

Rationale: the rest of the transport must NOT inline these. Centralising them here
means every value passes one provenance gate (see
``tests/independence/provenance.json`` + ``VALUES-PROVENANCE.md`` and the spec
``docs/protocol/HAIER-HON-TRANSPORT.md``). Each value is classified as:

  * OBSERVED       -- required verbatim by Haier's endpoints: any correct client MUST
                      send this exact value, so identity with another client (pyhOn
                      included) is interop, not copying. This is NOT a claim that addhOn
                      independently re-captured the literal off the wire -- several
                      coincide byte-for-byte with pyhOn precisely because Haier dictates
                      them (see tests/independence/provenance.json `source`).
  * CLIENT-CHOSEN  -- addhOn's own identity (not required verbatim by the server).
  * UNRESOLVED     -- a value that still needs an independent capture (APK / mitmproxy)
                      to be legitimately sourced. Kept working, but flagged so the
                      independence gate marks it expected-red until an owner captures it.

Spec anchors below (``HHT-secN``) point into docs/protocol/HAIER-HON-TRANSPORT.md.
This module is stdlib-only so it stays importable in isolation.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# -- OAuth / login surface (Salesforce-fronted) -- HHT-sec2 ---------------------
AUTH_API = "https://account2.hon-smarthome.com"   # OBSERVED: authorize/login host
APP = "hon"                                        # OBSERVED: custom redirect scheme
CLIENT_ID = (                                      # OBSERVED: Salesforce connected-app id
    "3MVG9QDx8IX8nP5T2Ha8ofvlmjLZl5L_gvfbT9."
    "HJvpHGKoAS_dcMN8LYpTSYeVFCraUnV.2Ag1Ki7m4znVO6"
)
OAUTH_RESPONSE_TYPE = "token+id_token"             # OBSERVED: implicit flow response_type
OAUTH_SCOPE = "api openid refresh_token web"       # OBSERVED: authorize scope (spaces kept)

# Host of AUTH_API, derived once (used for SSRF pinning of scraped hrefs). Not a
# free literal -- computed from AUTH_API, so it carries the same provenance.
AUTH_HOST = urlsplit(AUTH_API).netloc

# -- IoT command surface -- HHT-sec2 -------------------------------------------
API_URL = "https://api-iot.he.services"            # OBSERVED: IoT command API host

# -- Anonymous app-config surface (program-label catalog) -- HHT-sec2 ----------
# The appliance schema does NOT carry program LABELS: a startProgram category is named
# `PROGRAMS.<TYPE>.<PROGRAM>`, which is an i18n KEY. The hOn app resolves it against a
# translation catalog it downloads at runtime, so a client that wants readable program
# names must reproduce that anonymous two-step (app-config -> jsonPath -> catalog).
#
# Both values below were captured from the hOn 2.27.9 APK and then confirmed on the
# wire (2026-08-02): see apk/probe_translations_live.py, which records the offsets in
# apk/decomp.txt and the response matrix.
CONFIG_MICROSERVICE = "config"                     # OBSERVED: app-config path segment
# NOT A SECRET -- a PUBLIC CLIENT key, deliberately in source control.
#
# It identifies the client software, not a user or an account: every hOn installation
# ships this same value and anyone can read it out of the app. It grants no access to
# any account, carries no user data, and cannot be scoped per install -- the endpoint
# expects the CLIENT's key, so a user-supplied one would simply be rejected. Storing it
# in the config entry would add no security and would make the integration impossible to
# install. User credentials and session tokens are a different matter entirely: they are
# supplied at runtime and appear nowhere in this file. See the OBSERVED class definition
# in VALUES-PROVENANCE.md, which covers CLIENT_ID, AWS_ENDPOINT and AWS_AUTHORIZER the
# same way.
#
# OBSERVED: the gateway REJECTS the bare AWS api-key with 403 Forbidden. The app builds
# `x-api-key` as `AWS_API_KEY + "-" + apiKeySuffix`, and `getConfig` passes the suffix
# "configuration" -- so the suffixed form is the only one the endpoint accepts.
CONFIG_API_KEY = "GRCqFhC6Gk@ikWXm1RmnSmX1cm,MxY-configuration"

# -- AWS-IoT MQTT surface (custom authorizer) -- HHT-sec10 ---------------------
AWS_ENDPOINT = "a30f6tqw0oh1x0-ats.iot.eu-west-1.amazonaws.com"  # OBSERVED: IoT-Data ATS
AWS_AUTHORIZER = "candy-iot-authorizer"            # OBSERVED: custom-authorizer name

# -- HTTP header contract -- HHT-sec4 ------------------------------------------
CONTENT_TYPE = "application/json"                  # OBSERVED: JSON endpoints reject form bodies
# UNRESOLVED: the hOn app sends a real device/WebView User-Agent. pyhOn shipped the
# synthetic sentinel "Chrome/999.999.999.999"; addhOn inherited it. The cloud has
# accepted it for years, so it is kept to preserve wire behaviour, BUT it is NOT an
# independently-sourced value -- OWNER ACTION: capture the real UA from the hOn APK /
# a mitmproxy session and replace it (record the capture in VALUES-PROVENANCE.md).
# The independence provenance gate marks this row expected-red until then.
USER_AGENT = "Chrome/999.999.999.999"

# -- Client identity descriptor -- HHT-sec3 ------------------------------------
# CLIENT-CHOSEN: addhOn runs headless, so it presents a fixed identity. DEVICE_MODEL
# and MOBILE_ID are addhOn's own (pyhOn sent "pyhOn"). APP_VERSION / OS_VERSION track
# the current app but are still uncaptured placeholders (OWNER ACTION: confirm from
# the APK BuildConfig -- see VALUES-PROVENANCE.md).
APP_VERSION = "2.27.9"
OS_VERSION = 34
OS = "android"                                     # OBSERVED: os field value
DEVICE_MODEL = "addhon"
MOBILE_ID = "addhon"
