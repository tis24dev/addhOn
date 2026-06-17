"""Differential test del 2° pezzo del transport nativo: parse_appliance_list.

La logica di estrazione di pyhОn vive INLINE nel metodo async+HTTP
`api.load_appliances`, quindi non è importabile a sé: l'oracolo è la sua
trascrizione VERBATIM (`_pyhon_extract` sotto). Confrontiamo il nostro parser
contro l'oracolo su molte risposte; più i casi di DIVERGENZA VOLUTA dove pyhОn
crasha (catena di `.get()` su un intermedio non-dict) e noi ricadiamo su `[]`.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUR_PARSE = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "parse.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pyhon_extract(result):
    """Oracolo: trascrizione VERBATIM del parsing di pyhon api.load_appliances
    (meno il logging). NON importabile a sé perché inline in un metodo async+HTTP."""
    appliances = []
    if isinstance(result, dict):
        raw = (
            result.get("modules", {})
            .get("applianceList", {})
            .get("payload", {})
            .get("appliances", [])
        )
        if isinstance(raw, list):
            appliances = raw
        elif raw:
            pass  # pyhon qui logga un warning; per il confronto conta solo il ritorno
    return appliances


# Risposte ben formate / mancanti / vuote: il nostro parser DEVE dare lo stesso
# risultato di pyhОn.
_EQUAL = [
    {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}, {"b": 2}]}}}},
    {"modules": {"applianceList": {"payload": {"appliances": []}}}},
    {"modules": {"applianceList": {"payload": {"appliances": {"x": 1}}}}},  # non-lista truthy
    {"modules": {"applianceList": {"payload": {"appliances": 0}}}},          # non-lista falsy
    {"modules": {"applianceList": {"payload": {"appliances": None}}}},
    {"modules": {"applianceList": {"payload": {}}}},
    {"modules": {"applianceList": {}}},
    {"modules": {}},
    {},
    None,
    [],
    "x",
    123,
]

# Forme malformate con un livello intermedio NON-dict: pyhОn crasha
# (AttributeError), noi ricadiamo su [] (hardening voluto).
_HARDENED = [
    {"modules": "x"},
    {"modules": []},
    {"modules": None},                                       # None intermedio
    {"modules": {"applianceList": "y"}},
    {"modules": {"applianceList": []}},
    {"modules": {"applianceList": None}},
    {"modules": {"applianceList": {"payload": []}}},
    {"modules": {"applianceList": {"payload": "z"}}},
    {"modules": {"applianceList": {"payload": None}}},       # None intermedio (payload)
]


class ParseApplianceListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parse = _load(_OUR_PARSE, "addhon_transport_parse").parse_appliance_list

    def test_matches_pyhon_on_wellformed(self) -> None:
        for result in _EQUAL:
            with self.subTest(result=result):
                self.assertEqual(self.parse(result), _pyhon_extract(result))

    def test_pinned_real_shape(self) -> None:
        full = {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}, {"b": 2}]}}}}
        self.assertEqual(self.parse(full), [{"a": 1}, {"b": 2}])
        # ritorna la lista REALE (stesso oggetto, non una copia): come pyhОn
        self.assertIs(self.parse(full), full["modules"]["applianceList"]["payload"]["appliances"])

    def test_hardened_vs_pyhon_crash_on_intermediate_non_dict(self) -> None:
        for result in _HARDENED:
            with self.subTest(result=result):
                # pyhОn crasha su questi (documenta la fragilità che abbiamo tolto)...
                with self.assertRaises(AttributeError):
                    _pyhon_extract(result)
                # ...noi ricadiamo su [] (fail-safe).
                self.assertEqual(self.parse(result), [])


if __name__ == "__main__":
    unittest.main()
