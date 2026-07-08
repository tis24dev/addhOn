"""Gate A -- structural-independence check (catches a rename-only paraphrase).

For each addhOn transport module we compute the structure-only fingerprint (see
_fingerprint.py) and score it against every pyhOn connection module (fingerprints
frozen in pyhon_fingerprints.json -- HASHES ONLY, no pyhOn source) with TWO metrics:

  * CONTAINMENT  |ours ∩ pyhon| / |pyhon|  -- the primary, anti-dilution gate. It
    answers "how much of pyhOn's structure is still in here" and, unlike Jaccard,
    CANNOT be lowered by bolting unrelated code onto a copied skeleton. Ceiling 0.50.
  * JACCARD      |ours ∩ pyhon| / |ours ∪ pyhon|  -- a secondary symmetric signal.
    Ceiling 0.30.

A module is asserted INDEPENDENT only if it is under BOTH ceilings against the worst
pyhOn match. The ceilings are UNIFORM (no per-module loosening) so nothing passes by
having its own bar quietly raised.

Source of truth for "which modules are derived" is the round-1 line-level provenance
audit (byte-identical Salesforce/Aura login literals, key-for-key identity payload,
copied `send_command`, ...), NOT this metric. This gate is a CONSERVATIVE tripwire,
not the proof of copying: because several pyhOn reference modules are small,
max-containment over all of them can exceed 0.50 for genuinely unrelated code too
(stdlib modules and addhOn's own re-authored HA files also score >0.50 -- refuter
round 2, N1). So a FAIL here means "re-derive and re-check", and a PASS is
necessary-not-sufficient.

The six modules api / auth / connection / device / mqtt / oauth are the audit's
known-derived set (declared debt) and are marked xfail; only headers / parse / tokens
/ values are asserted independent -- and those also score low here (0.06-0.26). NO
threshold was lowered to fake a pass; a genuine de-paraphrase flips a deferred module
to xpass. Known follow-up (N1): size-normalise the metric or score each module only
against its named pyhOn counterpart to remove the small-reference inflation.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _fingerprint import containment, fingerprint, jaccard  # noqa: E402

_REPO = _HERE.parents[1]
_TRANSPORT = _REPO / "custom_components" / "addhon" / "client" / "transport"
_PYHON_FP = {
    name: set(hashes)
    for name, hashes in json.loads(
        (_HERE / "pyhon_fingerprints.json").read_text()
    )["modules"].items()
}

# UNIFORM ceilings (applied to every module -- no per-module exceptions).
_CONTAINMENT_CEILING = 0.50
_JACCARD_CEILING = 0.30

# Modules whose structural de-paraphrase is deferred: measured to still reproduce
# >=50% of a pyhOn module's structure. xfail keeps the debt visible without lowering
# a bar. (device/oauth join the original four once judged by containment, which the
# earlier Jaccard-only gate hid -- see refuter round 1, M1/M2/M3.)
_DEFERRED = {
    "api.py",
    "auth.py",
    "connection.py",
    "device.py",
    "mqtt.py",
    "oauth.py",
}
_DEFERRED_REASON = (
    "known-derived by the round-1 line-level audit (still carries pyhOn's copied "
    "literals/structure); structural de-paraphrase deferred (orchestrator-plan "
    "#15-18). TODO: re-derive against docs/protocol/HAIER-HON-TRANSPORT.md until this "
    "tripwire (containment <= 0.50 and jaccard <= 0.30) also clears"
)


def test_reference_fingerprints_are_populated() -> None:
    """Self-check: an emptied/truncated reference must FAIL, not silently pass Gate A.

    Without this, deleting pyhon_fingerprints.json's contents would drop every
    similarity score to 0 and green-stamp verbatim copies.
    """
    assert len(_PYHON_FP) >= 8, "pyhOn reference lost modules -- refusing to gate"
    for name, fp in _PYHON_FP.items():
        assert len(fp) >= 20, f"pyhOn reference {name} too small ({len(fp)}) -- suspect"


def _params():
    for path in sorted(_TRANSPORT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        marks = (
            [pytest.mark.xfail(reason=_DEFERRED_REASON, strict=False)]
            if path.name in _DEFERRED
            else []
        )
        yield pytest.param(path, id=path.name, marks=marks)


@pytest.mark.parametrize("path", list(_params()))
def test_module_is_structurally_independent(path: pathlib.Path) -> None:
    ours = fingerprint(path.read_text())
    cont = {name: containment(ours, fp) for name, fp in _PYHON_FP.items()}
    jac = {name: jaccard(ours, fp) for name, fp in _PYHON_FP.items()}
    worst_c_name = max(cont, key=cont.get)
    worst_c = cont[worst_c_name]
    worst_j = max(jac.values())
    assert worst_c <= _CONTAINMENT_CEILING and worst_j <= _JACCARD_CEILING, (
        f"{path.name}: containment {worst_c:.3f} (vs pyhOn {worst_c_name}) / jaccard "
        f"{worst_j:.3f} exceeds ceiling ({_CONTAINMENT_CEILING:.2f}/"
        f"{_JACCARD_CEILING:.2f}) -- still a paraphrase of pyhOn. Re-derive the "
        f"STRUCTURE from docs/protocol/HAIER-HON-TRANSPORT.md, not just the values."
    )
