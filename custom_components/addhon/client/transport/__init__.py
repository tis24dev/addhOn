# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native addhOn transport (auth/HTTP/MQTT).

Auth/transport layer covering the Haier cloud (unified-api, tokens). Pure pieces
(device descriptor, response parser), then HTTP/session and the auth flow
(Salesforce OAuth), then the MQTT client (awscrt).

NB: the data values (e.g. app version) are placeholders; the real values from the
APK reverse enter as a deliberate, separately validated step.
"""
