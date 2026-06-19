"""The Protocols that define the EXACT surface of the hOn client that the
integration depends on.

This is the contract the body of the integration rests on: the concrete objects of
the native client satisfy it by duck-typing. By capturing here ONLY what we really
use, the contract stays tiny and verifiable.

The surface below was measured against the real code (entities + hon_client):
`.value` (read/write), `.values`, range `.min/.max/.step`, command `.parameters`
+ `.send()` (+ `.categories`/`.category` for programs), appliance `.commands`/
`.attributes`/`.statistics`/`.appliance_type`/`.model_id`, session `.appliances`
+ async context manager. Nothing else.

CONSTRAINT: this module is dependency-free (only `typing`), so tests can assert
conformance with `isinstance(..., Protocol)`.
"""
from __future__ import annotations

from typing import Any, Awaitable, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Parameter(Protocol):
    """A command parameter (enum/fixed/program).

    `value` is readable and writable: writing validates the value and may
    raise ValueError (e.g. outside enumValues). `values` lists the allowed
    values (empty/trivial for non-enums). Used in number/switch/select/climate
    and in ac_command/hon_commands.
    """

    value: Any
    values: Sequence[str]


@runtime_checkable
class RangeParameter(Parameter, Protocol):
    """Continuous numeric parameter: adds the bounds. Used by number.py
    (param_range) for min/max/step read at runtime."""

    min: float
    max: float
    step: float


@runtime_checkable
class Command(Protocol):
    """A sendable command (settings/startProgram/stopProgram/...).

    `parameters` maps name->Parameter (you edit them before sending).
    `send()` transmits the command over the hOn channel. `categories`/`category`
    are used for program selection (HonParameterProgram) in select.py.
    """

    parameters: Mapping[str, Parameter]
    categories: Mapping[str, "Command"]
    category: str

    def send(self) -> Awaitable[bool]: ...


@runtime_checkable
class Appliance(Protocol):
    """An appliance: read state (`attributes`/`statistics`) + writable
    commands (`commands`). The metadata (`appliance_type`, `model_id`,
    `nick_name`) identify type/model. `update()` refreshes the state.

    Note: the integration reads the flat attributes via HonBaseEntity._get_attr,
    which draws from what hon_client extracts from this object.
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
    """The authenticated session: managed as an async context manager
    (`async with Session(email, password) as s: s.appliances`).

    `appliances` is the list (complete, including offline ones from the unified-api fix).
    It is built by the native auth/transport (`client.session.NativeHon`).
    """

    appliances: Sequence[Appliance]

    def __aenter__(self) -> Awaitable["HonSession"]: ...

    def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Awaitable[Any]: ...
