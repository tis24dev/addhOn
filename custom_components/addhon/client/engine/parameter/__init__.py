# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Parameter classes (range/enum/fixed) + base.

HonParameterEnum's setter compares on the normalized value, which prevents the
BABYCARE bug (a cloud-cased value being rejected against the already-clean list).
"""
