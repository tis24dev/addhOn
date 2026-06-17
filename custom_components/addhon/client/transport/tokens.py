"""Parsing dei token OAuth dalla redirect di login hОn (transport addhОn).

Riscrittura di `pyhon auth._parse_token_data`: dalla redirect
`.../mobilesdk/detect/oauth/done#access_token=...&refresh_token=...&id_token=...`
estrae i tre token via regex `nome=(.*?)&` (fino al primo `&`).

PRESERVAZIONE ESATTA (non hardening, a differenza del parser appliance-list):
i token vanno al cloud byte-identici e il flusso auth non è validabile offline,
quindi replichiamo alla lettera le quirk di pyhОn:
- solo `refresh_token` viene URL-decodificato (`unquote`); access/id restano grezzi;
- un token in fondo SENZA `&` finale NON viene catturato (la regex richiede il `&`);
- `complete` = tutti e tre i pattern HANNO matchato (anche se il valore catturato
  è vuoto, come il `bool(findall and ...)` di pyhОn), non "tutti i valori non vuoti".
Riscritta la STRUTTURA (helper data-driven + dataclass immutabile), preservato il
COMPORTAMENTO (verificato dal differential test contro pyhОn).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class OAuthTokens:
    """Token estratti dalla redirect OAuth. `complete` = tutti e tre presenti.

    NB: `cognito_token` NON è qui: arriva da una POST separata (token-refresh),
    non dalla redirect.
    """

    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    complete: bool = False


def parse_token_fragment(text: str) -> OAuthTokens:
    """Estrae access/refresh/id token dal testo della redirect OAuth."""

    def _match(name: str) -> str | None:
        found = re.findall(f"{name}=(.*?)&", text)
        return found[0] if found else None

    access = _match("access_token")
    refresh = _match("refresh_token")
    id_token = _match("id_token")
    return OAuthTokens(
        access_token=access or "",
        # Solo il refresh è URL-decodificato, come pyhОn.
        refresh_token=unquote(refresh) if refresh is not None else "",
        id_token=id_token or "",
        # Come pyhОn: conta che il pattern abbia MATCHATO (non che il valore sia
        # non vuoto), quindi `None not in (...)`.
        complete=None not in (access, refresh, id_token),
    )
