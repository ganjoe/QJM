"""Tests der Rahmung: der Rundendeckel kommt vom LAUF, nicht vom Dienst.

Bis Migration 032 galt SECRETARY_MAX_ROUNDS fuer alle Laeufe. Jetzt traegt ihn
das change_item — und framing.py muss ihn zeigen, sonst plant der Lead gegen
eine Grenze, die er nicht kennt.
"""
from __future__ import annotations

import unittest

from secretary import framing
from secretary.config import Config

ITEM = {
    "id": "11111111-1111-1111-1111-111111111111",
    "change_item_id": "22222222-2222-2222-2222-222222222222",
    "step_key": "review",
    "type": "review",
    "role": "lead_engineer",
    "round": 2,
    "payload": {"prompt": "x"},
    "budget": {},
}


class MaxRoundsTest(unittest.TestCase):

    def setUp(self) -> None:
        self.cfg = Config.from_env()

    def test_deckel_des_laufs_steht_in_der_rahmung(self) -> None:
        text = framing.build(ITEM, [], "Boss-Prompt", self.cfg, roles=[], max_rounds=5)
        self.assertIn("max_rounds: 5", text)
        self.assertIn("round >= 5", text)

    def test_ohne_laufwert_gilt_der_dienst_default(self) -> None:
        text = framing.build(ITEM, [], "Boss-Prompt", self.cfg, roles=[], max_rounds=None)
        self.assertIn(f"max_rounds: {self.cfg.max_rounds}", text)

    def test_rollenkatalog_nennt_das_reasoning_level(self) -> None:
        roles = [{"name": "cco", "max_concurrency": 2, "reasoning_effort": "low",
                  "description": "Social"}]
        text = framing.build(
            {**ITEM, "type": "initial"}, [], "Boss-Prompt", self.cfg, roles=roles)
        self.assertIn("Reasoning low", text)
        self.assertIn("reasoning_effort", text, "der Lead muss wissen, wie er es setzt")


if __name__ == "__main__":
    unittest.main()
