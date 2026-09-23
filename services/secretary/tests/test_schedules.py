"""Tests der Terminlogik.

schedules.py ist reine Rechnung — kein Datenbankzugriff, keine Uhr. Alle Tests
geben den Bezugszeitpunkt explizit mit, damit sie zu jeder Tageszeit dasselbe
Ergebnis liefern (auch an einem Zeitumstellungstag).

Ausfuehren:
  cd services/secretary && ./.venv/bin/python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from secretary import schedules

UTC = timezone.utc


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


class OnceTest(unittest.TestCase):
    """Einmalige (verzoegerte) Aufgaben."""

    def test_absoluter_termin_mit_offset(self) -> None:
        rule = schedules.parse({"kind": "once", "at": "2026-09-24T18:00:00+02:00"})
        self.assertEqual(
            schedules.first_occurrence(rule, at("2026-09-01T00:00:00+00:00")),
            at("2026-09-24T16:00:00+00:00"),
        )

    def test_nach_dem_termin_gibt_es_keinen_weiteren(self) -> None:
        rule = schedules.parse({"kind": "once", "at": "2026-09-24T18:00:00+02:00"})
        self.assertIsNone(schedules.next_occurrence(rule, at("2026-09-24T16:00:01+00:00")))

    def test_lokale_form_mit_zone(self) -> None:
        rule = schedules.parse({"kind": "once", "at": {
            "date": "2026-09-24", "time": "18:00", "time_zone": "Europe/Berlin"}})
        self.assertEqual(
            schedules.first_occurrence(rule, at("2026-09-01T00:00:00+00:00")),
            at("2026-09-24T16:00:00+00:00"),
        )

    def test_ohne_offset_wird_abgelehnt(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "once", "at": "2026-09-24T18:00:00"})

    def test_dst_luecke_wird_abgelehnt(self) -> None:
        # 02:30 gibt es am 29.03.2026 in Berlin nicht (02:00 -> 03:00).
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "once", "at": {
                "date": "2026-03-29", "time": "02:30", "time_zone": "Europe/Berlin"}})

    def test_max_runs_wird_uebernommen(self) -> None:
        rule = schedules.parse({"kind": "once", "at": "2026-09-24T18:00:00+02:00",
                                "max_runs": 1})
        self.assertEqual(rule["max_runs"], 1)


class DailyTest(unittest.TestCase):
    """Taegliche Wiederholung in einer Zone — der DST-Fall."""

    def rule(self) -> dict:
        return schedules.parse({"kind": "repeat", "every": "daily", "time": "18:00",
                                "time_zone": "Europe/Berlin"})

    def test_sommerzeit(self) -> None:
        # 24.09.2026 ist CEST (+2): 18:00 lokal = 16:00 UTC.
        self.assertEqual(
            schedules.next_occurrence(self.rule(), at("2026-09-24T12:00:00+00:00")),
            at("2026-09-24T16:00:00+00:00"),
        )

    def test_winterzeit(self) -> None:
        # 24.12.2026 ist CET (+1): 18:00 lokal = 17:00 UTC.
        self.assertEqual(
            schedules.next_occurrence(self.rule(), at("2026-12-24T12:00:00+00:00")),
            at("2026-12-24T17:00:00+00:00"),
        )

    def test_termin_ist_immer_strikt_in_der_zukunft(self) -> None:
        # Die wichtigste Eigenschaft: kein Nachhol-Sturm nach einem Ausfall.
        # Fuenf Tage Ausfall -> genau EIN Vorkommen, danach wieder Zukunft.
        rule = self.rule()
        now = at("2026-09-29T12:00:00+00:00")          # vier Tage zu spaet
        following = schedules.next_occurrence(rule, now)
        self.assertEqual(following, at("2026-09-29T16:00:00+00:00"))
        self.assertGreater(following, now)

    def test_am_termin_selbst_kommt_der_naechste_tag(self) -> None:
        self.assertEqual(
            schedules.next_occurrence(self.rule(), at("2026-09-24T16:00:00+00:00")),
            at("2026-09-25T16:00:00+00:00"),
        )


class WeeklyTest(unittest.TestCase):
    """Woechentliche Wiederholung — der Fall aus der Praxis (Sonntag 18:00)."""

    def rule(self) -> dict:
        return schedules.parse({"kind": "repeat", "every": "weekly", "weekday": 6,
                                "time": "18:00", "time_zone": "Europe/Berlin"})

    def test_naechster_sonntag(self) -> None:
        # 21.09.2026 ist ein Montag -> naechster Sonntag ist der 27.09.
        self.assertEqual(
            schedules.next_occurrence(self.rule(), at("2026-09-21T00:00:00+00:00")),
            at("2026-09-27T16:00:00+00:00"),
        )

    def test_strikt_nach_dem_termin_ist_naechste_woche(self) -> None:
        self.assertEqual(
            schedules.next_occurrence(self.rule(), at("2026-09-27T16:00:00+00:00")),
            at("2026-10-04T16:00:00+00:00"),
        )

    def test_wochentag_ist_pflicht(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "repeat", "every": "weekly", "time": "18:00"})

    def test_wochentag_bereich(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "repeat", "every": "weekly", "weekday": 7,
                             "time": "18:00"})


class FixedRateTest(unittest.TestCase):
    """Feste Rate, am Einrichtungszeitpunkt verankert."""

    def rule(self) -> dict:
        parsed = schedules.parse({"kind": "repeat", "every_seconds": 3600})
        return schedules.with_anchor(parsed, at("2026-09-21T12:00:00+00:00"))

    def test_erster_termin_ist_ein_intervall_spaeter(self) -> None:
        anchor = at("2026-09-21T12:00:00+00:00")
        self.assertEqual(schedules.next_occurrence(self.rule(), anchor),
                         at("2026-09-21T13:00:00+00:00"))

    def test_verpasste_intervalle_werden_uebersprungen(self) -> None:
        # 2,5 Intervalle zu spaet -> naechstes Intervall nach jetzt, nicht drei.
        self.assertEqual(
            schedules.next_occurrence(self.rule(), at("2026-09-21T14:30:00+00:00")),
            at("2026-09-21T15:00:00+00:00"),
        )

    def test_exakt_auf_dem_termin(self) -> None:
        self.assertEqual(
            schedules.next_occurrence(self.rule(), at("2026-09-21T15:00:00+00:00")),
            at("2026-09-21T16:00:00+00:00"),
        )

    def test_zu_kurze_rate_wird_abgelehnt(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "repeat", "every_seconds": 60})

    def test_ohne_anker_kein_termin(self) -> None:
        rule = schedules.parse({"kind": "repeat", "every_seconds": 3600})
        with self.assertRaises(schedules.ScheduleError):
            schedules.next_occurrence(rule, at("2026-09-21T12:00:00+00:00"))


class ParseTest(unittest.TestCase):
    """Die Regel wird geprueft, nicht geraten."""

    def test_unbekanntes_kind(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "cron", "at": "2026-01-01T00:00:00+00:00"})

    def test_unbekannte_zone(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "repeat", "every": "daily", "time": "08:00",
                             "time_zone": "Mars/Olympus"})

    def test_kaputte_uhrzeit(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({"kind": "repeat", "every": "daily", "time": "25:00"})

    def test_fehlt_komplett(self) -> None:
        with self.assertRaises(schedules.ScheduleError):
            schedules.parse({})

    def test_kanonische_form_enthaelt_nur_bekannte_felder(self) -> None:
        rule = schedules.parse({"kind": "repeat", "every": "daily", "time": "8:00",
                                "time_zone": "Europe/Berlin", "quatsch": 1})
        self.assertNotIn("quatsch", rule)
        self.assertEqual(rule["time"], "08:00")

    def test_default_zone(self) -> None:
        rule = schedules.parse({"kind": "repeat", "every": "daily", "time": "08:00"})
        self.assertEqual(rule["time_zone"], "Europe/Berlin")


class DescribeTest(unittest.TestCase):
    """Der Satz, den der Mensch liest."""

    def test_woechentlich(self) -> None:
        text = schedules.describe(schedules.parse(
            {"kind": "repeat", "every": "weekly", "weekday": 6, "time": "18:00"}))
        self.assertEqual(text, "woechentlich, Sonntag 18:00 (Europe/Berlin)")

    def test_taeglich(self) -> None:
        text = schedules.describe(schedules.parse(
            {"kind": "repeat", "every": "daily", "time": "08:30"}))
        self.assertEqual(text, "taeglich 08:30 (Europe/Berlin)")

    def test_feste_rate(self) -> None:
        text = schedules.describe(schedules.parse(
            {"kind": "repeat", "every_seconds": 3600}))
        self.assertEqual(text, "alle 1 Stunde(n)")

    def test_einmalig(self) -> None:
        text = schedules.describe(schedules.parse(
            {"kind": "once", "at": "2026-09-24T18:00:00+02:00"}))
        self.assertEqual(text, "einmalig 2026-09-24 16:00 UTC")


if __name__ == "__main__":
    unittest.main()
