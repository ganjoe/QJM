"""Tests der Rollen-Konfiguration (Reasoning-Level).

Zwei Dinge werden hier festgehalten, die leicht kaputtgehen:

1. Der RolePool haelt einen Prozess PRO (Rolle, Reasoning-Level). DSH setzt den
   Level bei der Initialisierung — ein Prozess traegt genau einen. Wuerde der
   Pool wieder nur nach Rolle schluesseln, bekaeme ein Item still das Level
   eines anderen.
2. Ein unbekannter Level muss VOR dem Datenbankzugriff abgelehnt werden, weil
   DSH ihn sonst erst beim Modellaufruf bemerkt.

Ausfuehren:
  cd services/secretary && ./.venv/bin/python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import unittest

from secretary import store
from secretary.config import Config
from secretary.harness import RolePool

ROLES_DIR = Config.from_env().roles_dir
PATCH = "roles/cco.cordis.yml"


@unittest.skipUnless(os.path.exists(os.path.join(ROLES_DIR, "cco.cordis.yml")),
                     "Rollen-Patch nicht vorhanden")
class RolePoolTest(unittest.TestCase):
    """Der Pool darf Rollen nicht ohne ihr Level wiederverwenden."""

    def setUp(self) -> None:
        self.pool = RolePool(Config.from_env())

    def tearDown(self) -> None:
        self.pool.close()

    def test_level_ist_teil_des_schluessels(self) -> None:
        low = self.pool.get("cco", PATCH, None, "low")
        high = self.pool.get("cco", PATCH, None, "high")
        self.assertIsNot(low, high, "zwei Level brauchen zwei Prozesse")
        self.assertIs(self.pool.get("cco", PATCH, None, "low"), low,
                      "dasselbe Level soll denselben Prozess wiederverwenden")

    def test_level_landet_im_harness_config(self) -> None:
        # DeepSeekHarnessConfig.reasoning_effort -> initialize(reasoningEffort)
        for level in ("off", "low", "high", "max"):
            runner = self.pool.get("cco", PATCH, None, level)
            self.assertEqual(runner._harness.config.reasoning_effort, level)

    def test_ohne_level_bleibt_der_dsh_default(self) -> None:
        runner = self.pool.get("cco", PATCH, None, None)
        self.assertIsNone(runner._harness.config.reasoning_effort)


class EffortValidationTest(unittest.TestCase):
    """Unbrauchbare Werte muessen vor dem Datenbankzugriff scheitern."""

    def test_unbekannter_level_wird_abgelehnt(self) -> None:
        with self.assertRaises(ValueError):
            store.set_role_effort("cco", "turbo")

    def test_erlaubte_level(self) -> None:
        self.assertEqual(set(store.REASONING_LEVELS), {"off", "low", "high", "max"})


if __name__ == "__main__":
    unittest.main()
