# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared snapshot/restore of engine parameter state for send-path rollback.

Assigning a value to a HonParameter (or applying a program) mutates the parameter
IN PLACE -- not only ``.value`` but also ``.values``/``.min``/``.max`` when the
assignment fires the command rules. If the subsequent ``command.send()`` fails we
must restore the FULL pre-mutation state, and do it by copying ``__dict__``
DIRECTLY (not via the setter) so the rules are NOT re-fired and the restricted
lists are restored too. A setter-based rollback would leave ``.values`` narrowed
and raise on revalidation, corrupting state that then contaminates later sends.

Centralized here so every send path (``hon_commands.async_send_command``,
``button``, ``switch``, ``program_options``) rolls back identically instead of
keeping four copies that could drift apart on future edge-case fixes.
"""
from __future__ import annotations


def snapshot_params(params) -> dict:
    """Shallow-copy the ``__dict__`` of every parameter in ``params``, keyed by name.

    Returns ``{}`` when ``params`` is not a dict. Parameters without a ``__dict__``
    are skipped (nothing to restore for them). The lists inside a parameter are
    REPLACED on mutation (never edited in place), so a shallow copy is enough.
    """
    if not isinstance(params, dict):
        return {}
    return {k: dict(p.__dict__) for k, p in params.items() if hasattr(p, "__dict__")}


def restore_params(params, snapshot) -> None:
    """Restore each snapshotted parameter's ``__dict__`` into ``params`` in place.

    Copies ``__dict__`` directly (bypassing the setter) so rules are NOT re-fired
    and ``values``/``min``/``max`` are restored too. No-op for a non-dict ``params``
    or a parameter that has since disappeared / lost its ``__dict__``.
    """
    if not isinstance(params, dict):
        return
    for key, saved in snapshot.items():
        param = params.get(key)
        if param is not None and hasattr(param, "__dict__"):
            param.__dict__.clear()
            param.__dict__.update(saved)
