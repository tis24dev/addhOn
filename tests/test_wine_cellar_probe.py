"""Issue #149: the shape-only reading of the hOn wine-cellar inventory API.

`client/transport/cellar.py` reduces a `cellar/v1` response to keys, types, counts
and three numbers of the first bottle; `HonApi.probe_wine_cellar` performs the two
read-only GETs and returns only those reductions. The user's wines (names,
wineries, vintages) must never come out of either.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import tests.test_command_catalog_transport  # noqa: E402,F401 - installs the HA/aiohttp stubs

from custom_components.addhon.client.transport import cellar  # noqa: E402
from custom_components.addhon.client.transport.api import HonApi  # noqa: E402

WINES_BODY = {
    "payload": {
        "wines": [
            {
                "name": "Chateau Secret 2010",
                "winery": "Private Estate",
                "quantity": 3,
                "price": "45.5",
                "zone": 1,
            },
            {"name": "Another", "quantity": 2},
        ],
        "Chateau Secret as a key": 1,
    }
}


class ShapeTest(unittest.TestCase):
    def test_json_shape_drops_every_string_value(self) -> None:
        shape = cellar.json_shape(WINES_BODY, numbers=False)
        text = json.dumps(shape)
        self.assertNotIn("Chateau Secret", text)
        self.assertNotIn("Private Estate", text)
        wines = shape["keys"]["payload"]["keys"]["wines"]
        self.assertEqual("list", wines["type"])
        self.assertEqual(2, wines["len"])
        self.assertEqual("int", wines["item"]["keys"]["quantity"])
        self.assertEqual(1, shape["keys"]["payload"]["other_keys"])

    def test_numbers_are_kept_only_when_asked(self) -> None:
        body = {"payload": {"count": 7, "ratio": 1.5, "flag": True}}
        kept = cellar.json_shape(body, numbers=True)["keys"]["payload"]["keys"]
        self.assertEqual({"type": "int", "value": 7}, kept["count"])
        self.assertEqual({"type": "float", "value": 1.5}, kept["ratio"])
        self.assertEqual("bool", kept["flag"])
        plain = cellar.json_shape(body, numbers=False)["keys"]["payload"]["keys"]
        self.assertEqual("int", plain["count"])

    def test_depth_and_key_bounds(self) -> None:
        deep: Any = 1
        for _ in range(10):
            deep = {"a": deep}
        self.assertIn("truncated", json.dumps(cellar.json_shape(deep, numbers=False)))
        wide = {f"k{i}": i for i in range(100)}
        shape = cellar.json_shape(wide, numbers=False)
        self.assertEqual(cellar.MAX_KEYS, len(shape["keys"]))
        self.assertEqual(100 - cellar.MAX_KEYS, shape["other_keys"])

    def test_wine_list_summary(self) -> None:
        summary = cellar.wine_list_summary(WINES_BODY)
        self.assertEqual("payload.wines", summary["wines_path"])
        self.assertEqual(2, summary["wines_count"])
        self.assertEqual({"quantity": 3, "price": "str", "zone": 1}, summary["first_wine"])
        self.assertEqual({"wines_path": "root", "wines_count": 0}, cellar.wine_list_summary([]))
        self.assertEqual({"wines_path": None}, cellar.wine_list_summary({"x": 1}))

    def test_number_refuses_bool_and_non_finite(self) -> None:
        self.assertIsNone(cellar.number(True))
        self.assertIsNone(cellar.number(float("nan")))
        self.assertIsNone(cellar.number(10**12))
        self.assertEqual(4, cellar.number(4))


class _Response:
    def __init__(self, body: Any, status: int) -> None:
        self._body = body
        self.status = status

    async def json(self, content_type: Any = None) -> Any:
        return self._body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _Connection:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any):
        self.calls.append(url)
        answer = self.answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return _Response(*answer)


WINES_URL = "https://api-iot.he.services/cellar/v1/3400477820460000/wines"
COUNT_URL = "https://api-iot.he.services/cellar/v1/wines/bottles-count"


class ProbeTest(unittest.TestCase):
    def test_two_gets_and_only_reductions(self) -> None:
        connection = _Connection(
            {WINES_URL: (WINES_BODY, 200), COUNT_URL: ({"payload": {"count": 5}}, 200)}
        )
        result = asyncio.run(HonApi(connection).probe_wine_cellar("3400477820460000"))
        self.assertEqual([WINES_URL, COUNT_URL], connection.calls)
        self.assertEqual("ok", result["wines"]["outcome"])
        self.assertEqual(200, result["wines"]["status"])
        self.assertEqual(2, result["wines"]["wines_count"])
        self.assertEqual(
            {"type": "int", "value": 5},
            result["bottles_count"]["shape"]["keys"]["payload"]["keys"]["count"],
        )
        self.assertNotIn("Chateau Secret", json.dumps(result))

    def test_the_key_is_quoted_into_one_path_segment(self) -> None:
        url = "https://api-iot.he.services/cellar/v1/a%2Fb/wines"
        connection = _Connection({url: ({}, 404), COUNT_URL: ({}, 404)})
        result = asyncio.run(HonApi(connection).probe_wine_cellar("a/b"))
        self.assertEqual(url, connection.calls[0])
        self.assertEqual(404, result["wines"]["status"])

    def test_one_failing_call_does_not_hide_the_other(self) -> None:
        connection = _Connection(
            {WINES_URL: RuntimeError("boom"), COUNT_URL: ({"payload": {"count": 0}}, 200)}
        )
        result = asyncio.run(HonApi(connection).probe_wine_cellar("3400477820460000"))
        self.assertEqual("error", result["wines"]["outcome"])
        self.assertTrue(str(result["wines"]["code"]).startswith("ADDHON-"))
        self.assertEqual("ok", result["bottles_count"]["outcome"])

    def test_cancellation_propagates(self) -> None:
        connection = _Connection({WINES_URL: asyncio.CancelledError()})
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(HonApi(connection).probe_wine_cellar("3400477820460000"))


if __name__ == "__main__":
    unittest.main()
