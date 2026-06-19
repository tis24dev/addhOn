"""I Protocol che definiscono l'ESATTA superficie del client hОn da cui dipende
l'integrazione.

È il contratto su cui poggia il corpo dell'integrazione: gli oggetti concreti del
client nativo lo soddisfano per duck-typing. Catturando qui SOLO ciò che usiamo
davvero, il contratto resta minuscolo e verificabile.

La superficie sotto è stata misurata sul codice reale (entità + hon_client):
`.value` (read/write), `.values`, range `.min/.max/.step`, command `.parameters`
+ `.send()` (+ `.categories`/`.category` per i programmi), appliance `.commands`/
`.attributes`/`.statistics`/`.appliance_type`/`.model_id`, session `.appliances`
+ context manager async. Nient'altro.

VINCOLO: questo modulo è SENZA dipendenze (solo `typing`), così i test possono
asserire la conformità con `isinstance(..., Protocol)`.
"""
from __future__ import annotations

from typing import Any, Awaitable, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Parameter(Protocol):
    """Un parametro di comando (enum/fixed/program).

    `value` è leggibile e scrivibile: la scrittura valida il valore e può
    sollevare ValueError (es. fuori dagli enumValues). `values` elenca i valori
    ammessi (vuoto/triviale per i non-enum). Usato in number/switch/select/climate
    e in ac_command/hon_commands.
    """

    value: Any
    values: Sequence[str]


@runtime_checkable
class RangeParameter(Parameter, Protocol):
    """Parametro numerico continuo: aggiunge i bound. Usato da number.py
    (param_range) per min/max/step letti a runtime."""

    min: float
    max: float
    step: float


@runtime_checkable
class Command(Protocol):
    """Un comando inviabile (settings/startProgram/stopProgram/...).

    `parameters` mappa nome->Parameter (li si modifica prima di inviare).
    `send()` trasmette il comando sul canale hОn. `categories`/`category`
    servono alla selezione programma (HonParameterProgram) in select.py.
    """

    parameters: Mapping[str, Parameter]
    categories: Mapping[str, "Command"]
    category: str

    def send(self) -> Awaitable[bool]: ...


@runtime_checkable
class Appliance(Protocol):
    """Un elettrodomestico: stato letto (`attributes`/`statistics`) + comandi
    scrivibili (`commands`). I metadati (`appliance_type`, `model_id`,
    `nick_name`) identificano tipo/modello. `update()` rinfresca lo stato.

    Nota: l'integrazione legge gli attributi flat via HonBaseEntity._get_attr,
    che attinge da ciò che hon_client estrae da questo oggetto.
    """

    commands: Mapping[str, Command]
    attributes: Mapping[str, Any]
    statistics: Mapping[str, Any]
    appliance_type: str
    model_id: Any
    nick_name: str

    def update(self) -> Awaitable[Any]: ...


@runtime_checkable
class HonSession(Protocol):
    """La sessione autenticata: gestita come context manager async
    (`async with Session(email, password) as s: s.appliances`).

    `appliances` è la lista (completa, inclusi offline dal fix unified-api).
    La costruisce l'auth/transport nativo (`client.session.NativeHon`).
    """

    appliances: Sequence[Appliance]

    def __aenter__(self) -> Awaitable["HonSession"]: ...

    def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Awaitable[Any]: ...
