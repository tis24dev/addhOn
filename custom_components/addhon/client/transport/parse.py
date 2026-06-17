"""Parser delle risposte del cloud hОn (transport addhОn).

Riscrittura della logica di estrazione della lista appliance da
`pyhon api.load_appliances` (il fix v2.7.1: endpoint
`POST /unified-api/v1/view/appliance-list`, che ritorna anche i device offline).

Forma della risposta: `result.modules.applianceList.payload.appliances` (lista).

Differenza VOLUTA rispetto a pyhОn: pyhОn estrae con una catena
`result.get("modules", {}).get("applianceList", {})...` che **solleva
AttributeError** se un livello intermedio non è un dict (es. `{"modules": "x"}`
o `{"modules": {"applianceList": []}}`), facendo fallire il setup. Qui
camminiamo difensivamente e qualsiasi forma inattesa ricade su `[]` (fail-safe),
così il chiamante tratta lo schema-drift come "0 appliance" invece di un crash.
Su tutte le risposte ben formate il risultato è identico a pyhОn (differential test).
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Percorso nella risposta POST /unified-api/v1/view/appliance-list.
_APPLIANCE_LIST_PATH = ("modules", "applianceList", "payload", "appliances")


def parse_appliance_list(result: Any) -> list:
    """Estrae la lista appliance (inclusi gli offline) dalla risposta unified-api.

    Ritorna la lista a `modules.applianceList.payload.appliances`. Qualsiasi forma
    inattesa (chiave mancante, livello intermedio non-dict, valore finale non-lista)
    -> `[]`. Un valore finale non-lista ma *truthy* = schema drift: log + `[]`.
    """
    node: Any = result
    for key in _APPLIANCE_LIST_PATH:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if isinstance(node, list):
        return node
    if node:
        _LOGGER.warning(
            "Risposta appliance-list: 'appliances' di tipo inatteso %s, ignorato",
            type(node).__name__,
        )
    return []
