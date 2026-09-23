"""Tests des Dashboard-Servers — ueber seine HTTP-Schnittstelle.

Laeuft gegen einen laufenden Server (Default http://127.0.0.1:8799). Ist keiner
erreichbar, werden die Tests uebersprungen statt rot.

Der Test legt eine STEHENDE AUFGABE mit einem Termin im Jahr 2030 an und schaltet
sie wieder ab. Sie kann deshalb nie feuern und kostet keine Modell-Tokens.

Ausfuehren:
  cd wiq_dashboard && ./.venv/bin/python -m unittest discover -s server/tests -t .
  WIQ_DASHBOARD_URL=http://10.20.0.23:8799 ...   gegen einen anderen Server
"""
from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request

BASE = os.environ.get("WIQ_DASHBOARD_URL", "http://127.0.0.1:8799").rstrip("/")


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return exc.code, body.decode(errors="replace")


class ServerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        try:
            status, health = call("GET", "/health")
        except OSError as exc:
            raise unittest.SkipTest(f"kein Server unter {BASE}: {exc}")
        if status != 200:
            raise unittest.SkipTest(f"Server antwortet {status}")
        cls.health = health

    def test_health_meldet_regelpruefung(self) -> None:
        self.assertEqual(self.health["status"], "healthy")
        self.assertTrue(self.health["rule_check"],
                        "schedules.py wurde nicht eingebunden — Regeln werden nicht geprueft")

    def test_runs_liefert_wurzeln_und_vorkommen(self) -> None:
        status, data = call("GET", "/api/runs?limit=5")
        self.assertEqual(status, 200)
        self.assertIn("roots", data)
        self.assertIn("occurrences", data)
        for row in data["roots"]:
            self.assertIsNone(row["source_change_item_id"], "Wurzeln haben keinen Vorfahren")

    def test_regel_wird_serverseitig_geprueft(self) -> None:
        # 02:30 am 28.03.2027 gibt es in Berlin nicht (Zeitumstellung).
        status, data = call("POST", "/api/schedule", {
            "prompt": "Test", "role": "cco",
            "rule": {"kind": "once", "at": {"date": "2027-03-28", "time": "02:30",
                                            "time_zone": "Europe/Berlin"}}})
        self.assertEqual(status, 400)
        self.assertIn("Zeitumstellung", json.dumps(data, ensure_ascii=False))

    def test_unbekannte_rolle_wird_abgelehnt(self) -> None:
        status, _ = call("POST", "/api/schedule", {
            "prompt": "Test", "role": "quatsch",
            "rule": {"kind": "repeat", "every": "daily", "time": "09:00"}})
        self.assertEqual(status, 400)

    def test_stehende_aufgabe_anlegen_und_abschalten(self) -> None:
        status, data = call("POST", "/api/schedule", {
            "prompt": "Selbsttest der HTTP-Schnittstelle (kann nie feuern).",
            "role": "cco", "title": "Selbsttest HTTP",
            "rule": {"kind": "once", "at": "2030-01-01T09:00:00+01:00"}})
        self.assertEqual(status, 200, data)
        definition_id = data["change_item_id"]
        try:
            status, detail = call("GET", f"/api/detail/{definition_id}")
            self.assertEqual(status, 200)
            self.assertTrue(detail["change_item"]["is_template"])
            self.assertEqual(detail["change_item"]["state"], "scheduled")
            # Genau eine Vorlage, noch kein Arbeitsschritt.
            self.assertEqual([i["type"] for i in detail["items"]], ["template"])

            status, _ = call("POST", f"/api/schedule/{definition_id}/cancel")
            self.assertEqual(status, 200)

            status, body = call("POST", f"/api/schedule/{definition_id}/cancel")
            self.assertEqual(status, 409, body)
        finally:
            call("POST", f"/api/schedule/{definition_id}/cancel")


if __name__ == "__main__":
    unittest.main()
