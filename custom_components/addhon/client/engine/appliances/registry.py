"""Registry per-tipo nativo (Fase 4 slice 4).

Sostituisce l'`importlib.import_module(f"...appliances.{type}")` dinamico di pyhОn con
una mappa STATICA tipo->classe: niente import a runtime, dipendenze esplicite, e l'IDE/
linter vede i riferimenti. La selezione segue pyhОn: chiave = `appliance_type.lower()`;
tipo senza classe per-tipo -> nessun extra (come il ModuleNotFoundError di pyhОn).
"""
from __future__ import annotations

from typing import Any, Optional, Type

from . import dw, ov, ref, td, wc, wd, wh, wm
from .base import ApplianceExtra

# chiave = appliance_type.lower() (gli stessi tipi che pyhОn aveva in appliances/*.py)
_REGISTRY: dict[str, Type[ApplianceExtra]] = {
    "dw": dw.Appliance,
    "ov": ov.Appliance,
    "ref": ref.Appliance,
    "td": td.Appliance,
    "wc": wc.Appliance,
    "wd": wd.Appliance,
    "wh": wh.Appliance,
    "wm": wm.Appliance,
}


def get_extra(appliance: Any) -> Optional[ApplianceExtra]:
    """Istanzia l'extra per-tipo dell'appliance, o None se il tipo non ne ha uno
    (come pyhОn quando il modulo per-tipo non esiste)."""
    cls = _REGISTRY.get(str(appliance.appliance_type).lower())
    return cls(appliance) if cls is not None else None
