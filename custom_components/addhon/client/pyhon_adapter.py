"""Adattatore-ponte verso il pyhОn vendorizzato (transizione).

Durante la migrazione questo è l'UNICO file di `client/` che importa
`_vendor.pyhon` (vedi MIGRATION.md, regola 1). Il corpo dell'integrazione
(`hon_client.py`) ottiene la sessione hОn DA QUI, non più con un import diretto
di `_vendor.pyhon`: così è disaccoppiato da pyhОn dietro questa funzione, e
quando arriverà il transport nativo si cambia solo qui.

`create_session` ritorna un oggetto conforme a `interfaces.HonSession`
(oggi: `pyhon.Hon`; domani: il client nativo).
"""
from __future__ import annotations

from typing import Any


def create_session(email: str, password: str) -> Any:
    """Crea la sessione hОn autenticabile (context manager async).

    Il chiamante la usa via `__aenter__()` e ne legge `.appliances`, esattamente
    come prima. L'import di pyhОn è lazy (avviene solo qui, alla creazione) e
    riporta il messaggio amichevole se la libreria manca.
    """
    try:
        from .._vendor.pyhon import Hon
    except ImportError as err:  # pragma: no cover - solo se il vendor manca
        raise ImportError("La libreria pyhOn non è installata.") from err

    return Hon(email=email, password=password)
