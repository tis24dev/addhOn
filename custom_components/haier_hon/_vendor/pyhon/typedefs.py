from typing import Union, Any, TYPE_CHECKING, Protocol

import aiohttp
from yarl import URL

if TYPE_CHECKING:
    from custom_components.haier_hon._vendor.pyhon.parameter.base import HonParameter
    from custom_components.haier_hon._vendor.pyhon.parameter.enum import HonParameterEnum
    from custom_components.haier_hon._vendor.pyhon.parameter.fixed import HonParameterFixed
    from custom_components.haier_hon._vendor.pyhon.parameter.program import HonParameterProgram
    from custom_components.haier_hon._vendor.pyhon.parameter.range import HonParameterRange


class Callback(Protocol):  # pylint: disable=too-few-public-methods
    def __call__(
        self, url: str | URL, *args: Any, **kwargs: Any
    ) -> aiohttp.client._RequestContextManager: ...


Parameter = Union[
    "HonParameter",
    "HonParameterRange",
    "HonParameterEnum",
    "HonParameterFixed",
    "HonParameterProgram",
]
