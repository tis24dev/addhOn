"""Flusso OAuth login hОn — pezzi PURI (transport addhОn).

Riscrittura dei pezzi deterministici di `pyhon auth.HonAuth._introduce`:
- `build_authorize_url(nonce)`: l'URL di authorize Salesforce;
- `is_oauth_done(text)` / `extract_login_url(text)`: l'analisi della pagina authorize.

EXACT-PRESERVING: l'URL e l'encoding "a mano" dei parametri (scope con spazi NON
encodati, `redirect_uri` pre-quotato) sono il contratto che il server si aspetta;
vanno al cloud byte-identici. Costanti inline (come gli altri moduli transport);
rispecchiano oggi pyhon const, pinnate dal differential test contro il vero const.py.
L'orchestrazione HTTP che usa questi pezzi sta nel session/auth nativo (validato live).
"""
from __future__ import annotations

import re
from urllib.parse import quote

# Endpoint/identificatori (valori-dato che rispecchiano pyhon const).
AUTH_API = "https://account2.hon-smarthome.com"
APP = "hon"
CLIENT_ID = (
    "3MVG9QDx8IX8nP5T2Ha8ofvlmjLZl5L_gvfbT9."
    "HJvpHGKoAS_dcMN8LYpTSYeVFCraUnV.2Ag1Ki7m4znVO6"
)

# Estrae il primo link `url='...'` o `href='...'` dalla pagina authorize.
_LOGIN_URL_RE = re.compile("(?:url|href) ?= ?'(.+?)'")


def build_authorize_url(nonce: str) -> str:
    """URL di authorize OAuth (login mobile). `nonce` generato dal chiamante."""
    redirect_uri = quote(f"{APP}://mobilesdk/detect/oauth/done")
    params = {
        "response_type": "token+id_token",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "display": "touch",
        "scope": "api openid refresh_token web",
        "nonce": nonce,
    }
    # Join "a mano" come pyhon: NIENTE urlencode (scope tiene gli spazi).
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{AUTH_API}/services/oauth2/authorize/expid_Login?{query}"


def is_oauth_done(text: str) -> bool:
    """True se la pagina authorize è GIÀ la redirect coi token (login non serve).

    PRECEDENZA per l'orchestrazione: chiamare PRIMA `extract_login_url`; consultare
    `is_oauth_done` SOLO se l'estrazione ritorna None (come pyhОn: se trova il
    login_url ignora la presenza di oauth/done).
    """
    return "oauth/done#access_token=" in text


def extract_login_url(text: str) -> str | None:
    """URL di login dalla pagina authorize, o None se assente.

    Il relativo `/NewhOnLogin...` (login page nuova da lug-2024) viene riscritto
    sull'endpoint vecchio `/s/login...`, come fa pyhОn per evitare la nuova pagina.
    """
    matches = _LOGIN_URL_RE.findall(text)
    if not matches:
        return None
    url = matches[0]
    if url.startswith("/NewhOnLogin"):
        url = f"{AUTH_API}/s/login{url}"
    return url
