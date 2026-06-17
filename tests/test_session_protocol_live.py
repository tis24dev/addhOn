"""Verifica LIVE che il vero pyhon.Hon soddisfi il Protocol HonSession del seam.

È il controllo "carico-portante" dello strangle della sessione: l'adapter ritorna
un pyhon.Hon, e tutto il piano si regge sul fatto che quell'oggetto sia conforme a
client/interfaces.HonSession.

Richiede aiohttp/awsiotsdk (le dipendenze runtime di pyhОn): se assenti (es. CI
unit senza HA), il test si SALTA in modo pulito. Quando le dipendenze ci sono
(ambiente HA reale, o il venv /tmp/hon-dump-venv), gira davvero. L'import del Hon
reale avviene in un SUBPROCESS isolato per non inquinare sys.modules del processo
pytest condiviso (il trucco pre-registra package vuoti per saltare il pesante
__init__ dell'integrazione).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _deps_available() -> bool:
    # Importare il Hon reale richiede aiohttp E awscrt (hon.py importa mqtt.py
    # che fa `from awscrt import mqtt5`). Servono entrambi o il subprocess
    # fallirebbe l'import invece di saltare.
    return all(importlib.util.find_spec(m) is not None for m in ("aiohttp", "awscrt"))


class LiveSessionProtocolTest(unittest.TestCase):
    def test_real_hon_satisfies_honsession(self) -> None:
        if not _deps_available():
            self.skipTest("aiohttp/awscrt non disponibili: salto la verifica del Hon reale")
        script = textwrap.dedent(
            f"""
            import sys, types, importlib.util
            from pathlib import Path
            root = Path({str(_ROOT)!r}); sys.path.insert(0, str(root))
            for pkg in ("custom_components", "custom_components.addhon"):
                m = types.ModuleType(pkg)
                m.__path__ = [str(root / pkg.replace(".", "/"))]
                sys.modules[pkg] = m
            spec = importlib.util.spec_from_file_location(
                "ifc", root / "custom_components/addhon/client/interfaces.py")
            ifc = importlib.util.module_from_spec(spec); spec.loader.exec_module(ifc)
            from custom_components.addhon._vendor.pyhon import Hon
            h = Hon(email="x@example.com", password="y")
            assert isinstance(h, ifc.HonSession), "Hon NON conforme a HonSession"
            print("CONFORME")
            """
        )
        res = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(res.returncode, 0, f"stderr:\n{res.stderr}")
        self.assertIn("CONFORME", res.stdout)


if __name__ == "__main__":
    unittest.main()
