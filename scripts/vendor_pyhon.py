#!/usr/bin/env python3
"""Rigenera la copia vendorizzata di pyhon dentro l'integrazione.

pyhon è mantenuta nel fork privato telard-pixel/pyhon (sorgente di verità) e
vendorizzata qui sotto custom_components/haier_hon/_vendor/pyhon/ perché un
repo privato non è pip-installabile da Home Assistant. Questo script riallinea
la copia vendorizzata al fork e riscrive gli import per isolarla (namespace
custom_components.haier_hon._vendor.pyhon), così non collide con un'eventuale
pyhon "vanilla" installata da un'altra integrazione.

Uso:
    python scripts/vendor_pyhon.py [--ref main|vX.Y.Z] [--source PATH]

--source PATH  usa un checkout locale del fork (la cartella che contiene
               pyhon/) invece di clonare. Senza --source viene clonato
               https://github.com/telard-pixel/pyhon al ref indicato (richiede
               credenziali git per il repo privato).
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

FORK_URL = "https://github.com/telard-pixel/pyhon"
NEW_PKG = "custom_components.haier_hon._vendor.pyhon"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(REPO_ROOT, "custom_components", "haier_hon", "_vendor")
DST_PKG = os.path.join(VENDOR_DIR, "pyhon")


def _resolve_source(ref: str, source: str | None) -> tuple[str, str, str]:
    """Restituisce (path_radice_sorgente, ref, commit_sha)."""
    if source:
        root = os.path.abspath(source)
        if not os.path.isdir(os.path.join(root, "pyhon")):
            sys.exit(f"--source {root} non contiene una cartella pyhon/")
        sha = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True
        ).stdout.strip() or "(working tree)"
        return root, ref, sha

    tmp = tempfile.mkdtemp(prefix="pyhon-vendor-")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", ref, FORK_URL, tmp],
            check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            f"clone di {FORK_URL}@{ref} fallito (repo privato: servono "
            f"credenziali git). In alternativa usa --source su un checkout locale."
        )
    sha = subprocess.run(
        ["git", "-C", tmp, "rev-parse", "HEAD"],
        capture_output=True, text=True
    ).stdout.strip()
    return tmp, ref, sha


def _rewrite_imports() -> int:
    """Namespacizza gli import nei file vendorizzati. Ritorna i file toccati."""
    touched = 0
    for root, _, files in os.walk(DST_PKG):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8") as fh:
                original = fh.read()
            updated = original.replace("from pyhon", "from " + NEW_PKG)
            updated = updated.replace(
                'f"pyhon.appliances.', 'f"' + NEW_PKG + ".appliances."
            )
            if updated != original:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(updated)
                touched += 1
    return touched


def _sanity_check() -> list[str]:
    """Cerca riferimenti a `pyhon` non namespacizzati (esclude i falsi positivi)."""
    leftovers: list[str] = []
    for root, _, files in os.walk(DST_PKG):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    stripped = line.lstrip()
                    if stripped.startswith("from pyhon") or 'f"pyhon.' in line:
                        leftovers.append(f"{os.path.relpath(path, REPO_ROOT)}:{i}")
    return leftovers


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="main", help="ref/tag del fork (default: main)")
    ap.add_argument("--source", help="checkout locale del fork (salta il clone)")
    args = ap.parse_args()

    src_root, ref, sha = _resolve_source(args.ref, args.source)
    src_pkg = os.path.join(src_root, "pyhon")
    src_license = os.path.join(src_root, "LICENSE")

    # ricrea _vendor da zero
    shutil.rmtree(VENDOR_DIR, ignore_errors=True)
    os.makedirs(VENDOR_DIR, exist_ok=True)
    with open(os.path.join(VENDOR_DIR, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write('"""Dipendenze di terze parti vendorizzate (vedi i LICENSE).\n')
        fh.write("Rigenerato da scripts/vendor_pyhon.py: non modificare a mano.\n")
        fh.write('"""\n')
    shutil.copytree(src_pkg, DST_PKG)
    if os.path.isfile(src_license):
        shutil.copy(src_license, os.path.join(DST_PKG, "LICENSE"))

    touched = _rewrite_imports()
    leftovers = _sanity_check()
    if leftovers:
        sys.exit("RIFERIMENTI 'pyhon' NON NAMESPACIZZATI:\n  " + "\n  ".join(leftovers))

    stamp = datetime.date.today().isoformat()
    with open(os.path.join(VENDOR_DIR, "VENDOR.md"), "w", encoding="utf-8") as fh:
        fh.write("# Vendored dependencies\n\n")
        fh.write("Rigenerato da `scripts/vendor_pyhon.py` (non modificare a mano).\n\n")
        fh.write("## pyhon\n\n")
        fh.write(f"- source: {FORK_URL}\n")
        fh.write(f"- ref: {ref}\n")
        fh.write(f"- commit: {sha}\n")
        fh.write(f"- vendored: {stamp}\n")
        fh.write(f"- import namespace: `{NEW_PKG}`\n")

    n_py = sum(1 for r, _, fs in os.walk(DST_PKG) for f in fs if f.endswith(".py"))
    print(f"OK: vendorizzato pyhon @ {ref} ({sha[:10]})")
    print(f"    file .py: {n_py} | import riscritti: {touched}")
    print(f"    -> {os.path.relpath(DST_PKG, REPO_ROOT)}")


if __name__ == "__main__":
    main()
