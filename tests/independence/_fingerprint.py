# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Structure-only AST fingerprinting for the independence harness (Gate A).

The goal is to detect a *paraphrase* of pyhOn: code whose control-flow / call
structure was line-ported even though identifiers and string/number literals were
renamed. We therefore reduce a module to STRUCTURE ONLY -- the ordered sequence of
AST *node types* and their nesting -- discarding every identifier, attribute name,
argument name and literal value. Two consequences follow:

  * the legitimately shared, Haier-mandated constants and endpoints (policed
    separately by the provenance gate, Gate B) do NOT inflate the similarity score,
    because their VALUES are erased; and
  * a rename-only paraphrase still scores high, because the tree SHAPE survives.

We compare two modules by the Jaccard overlap of their k-gram hash sets over that
node-type token stream. A high overlap is a strong copy signal; an independently
authored module scores low even when it must emit the same wire bytes.

Why node types rather than ``ast.dump``: ``ast.dump``'s textual format and field
set drift between CPython releases, which would make a fingerprint frozen on one
interpreter incomparable on another. A pre-order walk that emits only
``type(node).__name__`` (plus explicit nesting brackets) is stable across the
CPython versions this project runs on (see ``pyhon_fingerprints.json._generator``),
so the frozen pyhOn reference stays comparable to freshly computed addhOn
fingerprints in CI.

IMPORTANT: this file and the checked-in ``pyhon_fingerprints.json`` store NO pyhOn
source, identifiers or literal values -- only opaque structural hash strings -- so
the anti-copy reference is itself non-derivative.
"""
from __future__ import annotations

import ast
import hashlib


def _strip_docstrings(tree: ast.AST) -> None:
    """Drop module/class/function docstrings so prose does not count as structure."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]


def _walk(node: ast.AST, out: list[str]) -> None:
    out.append(type(node).__name__)
    out.append("(")
    for child in ast.iter_child_nodes(node):
        _walk(child, out)
    out.append(")")


def structure_tokens(src: str) -> list[str]:
    """Node-type token stream of ``src`` (identifiers/attrs/literals all erased).

    Only ``type(node).__name__`` and nesting brackets are emitted, so a ``Name``
    contributes ``Name`` regardless of the identifier and a ``Constant``
    contributes ``Constant`` regardless of the value -- structure only.
    """
    tree = ast.parse(src)
    _strip_docstrings(tree)
    out: list[str] = []
    _walk(tree, out)
    return out


def fingerprint(src: str, k: int = 9) -> set[str]:
    """Set of k-gram hashes over the structure-only token stream of ``src``."""
    toks = structure_tokens(src)
    if len(toks) < k:
        grams = [" ".join(toks)] if toks else []
    else:
        grams = (" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1))
    return {hashlib.sha1(g.encode()).hexdigest()[:12] for g in grams}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(ours: set[str], reference: set[str]) -> float:
    """Fraction of the pyhOn REFERENCE structure reproduced in ``ours``.

    ``|ours ∩ reference| / |reference|``. Unlike Jaccard, this does NOT shrink when
    ``ours`` grows: appending unrelated code (new MFA/SSRF logic, a bigger file)
    cannot dilute a copied skeleton below the line. It is the honest anti-copy
    metric -- "how much of pyhOn is still in here" -- and is the primary gate; Jaccard
    is kept as a secondary, symmetric signal.
    """
    if not reference:
        return 0.0
    return len(ours & reference) / len(reference)
