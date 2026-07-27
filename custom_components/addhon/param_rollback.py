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


def restore_owned_params(params, baseline, own_write) -> None:
    """Compare-and-restore: undo only the caller's OWN pre-await mutations.

    Used where the snapshot/restore window spans an ``await`` (a network send),
    during which another thread (the MQTT push callback) can mutate the SAME
    parameter objects outside this module's caller's lock. A blind
    ``restore_params`` there would clobber that concurrent, authoritative
    update with the stale pre-send snapshot.

    ``own_write`` is a second snapshot taken right after the caller's own
    mutation and before the awaited call. A parameter is restored to
    ``baseline`` only if its CURRENT ``__dict__`` still equals ``own_write`` --
    proof nothing else touched it since. If it differs, something else wrote to
    it during the await, and that write is left in place. A key absent from
    ``own_write`` was never touched by the caller (``own_write`` falls back to
    ``baseline`` for it), so restoring is then only ever a no-op unless a
    concurrent write landed on an otherwise-untouched key -- in which case it is
    likewise preserved.
    """
    if not isinstance(params, dict):
        return
    for key, saved in baseline.items():
        param = params.get(key)
        if param is None or not hasattr(param, "__dict__"):
            continue
        expected = own_write.get(key, saved)
        if param.__dict__ != expected:
            continue
        param.__dict__.clear()
        param.__dict__.update(saved)
