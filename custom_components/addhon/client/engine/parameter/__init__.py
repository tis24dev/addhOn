# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Parameter classes (range/enum/fixed) + base.

HonParameterEnum's setter compares on the normalized value, which prevents the
BABYCARE bug (a cloud-cased value being rejected against the already-clean list).
"""
