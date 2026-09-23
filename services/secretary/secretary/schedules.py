"""Zeitregeln stehender Aufgaben (Klasse B).

Das ist die EINZIGE Stelle im System, an der ein Termin berechnet wird. Der
MCP-Server schreibt nur die Regel in die Datenbank; die Sekretaerin schaerft
(next_run_at setzen) und feuert. Deshalb gibt es keine zweite, abweichende
Terminlogik in TypeScript.

Regelformen (so stehen sie in change_items.schedule):

  {"kind": "once",   "at": "2026-09-24T18:00:00+02:00"}
  {"kind": "once",   "at": {"date": "2026-09-24", "time": "18:00",
                            "time_zone": "Europe/Berlin"}}
  {"kind": "repeat", "every": "daily",  "time": "18:00", "time_zone": "Europe/Berlin"}
  {"kind": "repeat", "every": "weekly", "weekday": 6, "time": "18:00",
                      "time_zone": "Europe/Berlin"}
  {"kind": "repeat", "every_seconds": 3600}

  optional in jedem Fall: "max_runs": 10

Zeitsemantik (bewusst, nicht implizit):

* Alle Termine sind Instants. Gespeichert wird timestamptz, gerechnet wird in
  der Zeitzone der Regel. Die Datenbank laeuft in UTC — die Zone steht in der
  Regel, nicht in der Umgebung.
* "strikt nach" ist die Regel: next_occurrence() liefert immer einen Termin in
  der Zukunft. Dadurch gibt es KEINEN Nachhol-Sturm, wenn die Sekretaerin
  laenger aus war: ein verpasster Termin feuert genau einmal (verspaetet), der
  Rest wird uebersprungen. Ein verpasster Lauf ist besser als keiner.
* DST-Luecke (Fruehjahr): eine Wanduhrzeit, die es nicht gibt, wird auf den
  Instant direkt nach der Umstellung gelegt (PEP 495, fold=0). DST-Ueberlappung
  (Herbst): der fruehere der beiden Instants gewinnt. Beides ist deterministisch
  und wird nicht geraten.
* weekday: 0 = Montag ... 6 = Sonntag (ISO).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Eine stehende Aufgabe kostet pro Vorkommen echte Modell-Tokens. Fuenf Minuten
# ist die Untergrenze (dieselbe Grenze, die DSH seinem Schedule-Paket setzt).
MIN_EVERY_SECONDS = 300

DEFAULT_TIME_ZONE = "Europe/Berlin"

_WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
                "Samstag", "Sonntag")


class ScheduleError(ValueError):
    """Die Regel ist unbrauchbar. Die Definition wird sichtbar failed."""


# ── Hilfen ─────────────────────────────────────────────────────────────────

def _zone(rule: dict[str, Any]) -> ZoneInfo:
    name = str(rule.get("time_zone") or DEFAULT_TIME_ZONE)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unbekannte Zeitzone {name!r}") from exc


def _parse_clock(value: Any) -> tuple[int, int]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"Uhrzeit {text!r} ist nicht HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ScheduleError(f"Uhrzeit {text!r} ist nicht HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"Uhrzeit {text!r} liegt ausserhalb 00:00-23:59")
    return hour, minute


def _local_instant(naive: datetime, zone: tzinfo) -> datetime:
    """Wanduhrzeit -> Instant. Doppelte Zeiten (DST-Ueberlappung) nehmen den
    frueheren der beiden Instants (PEP 495, fold=0)."""
    return naive.replace(tzinfo=zone, fold=0)


def _exists_locally(naive: datetime, zone: tzinfo) -> bool:
    """False, wenn diese Wanduhrzeit in dieser Zone nicht vorkommt (DST-Luecke)."""
    aware = _local_instant(naive, zone)
    back = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    return back == naive


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


# ── Regel pruefen und kanonisieren ─────────────────────────────────────────

def parse(rule: Any) -> dict[str, Any]:
    """Prueft die Regel und gibt die kanonische Form zurueck. Wirft ScheduleError."""
    if not isinstance(rule, dict):
        raise ScheduleError("schedule ist kein Objekt")
    kind = str(rule.get("kind") or "").strip()
    if kind == "once":
        at = _parse_at(rule.get("at"))
        out: dict[str, Any] = {"kind": "once", "at": at.isoformat()}
        _copy_max_runs(rule, out)
        return out
    if kind == "repeat":
        if rule.get("every_seconds") is not None:
            try:
                seconds = int(rule["every_seconds"])
            except (TypeError, ValueError) as exc:
                raise ScheduleError("every_seconds ist keine Zahl") from exc
            if seconds < MIN_EVERY_SECONDS:
                raise ScheduleError(
                    f"every_seconds={seconds} ist zu kurz — Minimum ist {MIN_EVERY_SECONDS} "
                    "(jedes Vorkommen kostet Modell-Tokens)")
            out = {"kind": "repeat", "every_seconds": seconds}
            _copy_max_runs(rule, out)
            return out
        every = str(rule.get("every") or "").strip()
        if every not in ("daily", "weekly"):
            raise ScheduleError("every muss 'daily' oder 'weekly' sein (oder every_seconds)")
        hour, minute = _parse_clock(rule.get("time"))
        zone = _zone(rule)
        out = {"kind": "repeat", "every": every, "time": f"{hour:02d}:{minute:02d}",
               "time_zone": str(zone.key)}
        if every == "weekly":
            weekday = rule.get("weekday")
            if weekday is None:
                raise ScheduleError("weekly braucht weekday (0=Montag ... 6=Sonntag)")
            try:
                weekday = int(weekday)
            except (TypeError, ValueError) as exc:
                raise ScheduleError("weekday ist keine Zahl") from exc
            if not (0 <= weekday <= 6):
                raise ScheduleError("weekday muss 0..6 sein (0=Montag)")
            out["weekday"] = weekday
        _copy_max_runs(rule, out)
        return out
    raise ScheduleError("kind muss 'once' oder 'repeat' sein")


def _copy_max_runs(rule: dict[str, Any], out: dict[str, Any]) -> None:
    value = rule.get("max_runs")
    if value is None or value == "":
        return
    try:
        max_runs = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleError("max_runs ist keine Zahl") from exc
    if max_runs < 1:
        raise ScheduleError("max_runs muss mindestens 1 sein")
    out["max_runs"] = max_runs


def _parse_at(value: Any) -> datetime:
    """Absoluter Termin: RFC-3339 MIT Offset, oder {date,time,time_zone}."""
    if isinstance(value, dict):
        date_text = str(value.get("date") or "").strip()
        time_text = str(value.get("time") or "").strip()
        zone = _zone(value)
        try:
            naive = datetime.fromisoformat(f"{date_text}T{time_text}")
        except ValueError as exc:
            raise ScheduleError(f"Datum/Uhrzeit {date_text!r} {time_text!r} ist unlesbar") from exc
        # Ein EINMALIGER Termin wird nicht geraten: wer 02:30 sagt, meint 02:30.
        # Bei wiederkehrenden Regeln wird dieselbe Luecke aufgeloest (siehe unten),
        # weil dort ein Ausfall ein ganzes Vorkommen kosten wuerde.
        if not _exists_locally(naive, zone):
            raise ScheduleError(
                f"{date_text} {time_text} existiert in {zone.key} nicht (Zeitumstellung) — "
                "andere Uhrzeit waehlen")
        return _local_instant(naive, zone)
    text = str(value or "").strip()
    if not text:
        raise ScheduleError("once braucht 'at'")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleError(f"at {text!r} ist kein ISO-8601-Zeitpunkt") from exc
    if parsed.tzinfo is None:
        raise ScheduleError(
            "at braucht einen Zeitzonen-Offset (z.B. 2026-09-24T18:00:00+02:00) "
            "oder die Form {date, time, time_zone}")
    return parsed


# ── Termine ────────────────────────────────────────────────────────────────

def first_occurrence(rule: dict[str, Any], anchor: datetime) -> datetime:
    """Erster Termin dieser Regel. anchor = Zeitpunkt der Einrichtung (created_at)."""
    kind = rule.get("kind")
    if kind == "once":
        return _to_utc(datetime.fromisoformat(str(rule["at"])))
    return next_occurrence(rule, anchor)


def next_occurrence(rule: dict[str, Any], after: datetime) -> datetime | None:
    """Erster Termin STRIKT nach 'after'. None = diese Regel hat keine weiteren."""
    kind = rule.get("kind")
    if kind == "once":
        return None                                  # genau ein Vorkommen
    if kind == "repeat" and "every_seconds" in rule:
        seconds = int(rule["every_seconds"])
        anchor = rule.get("_anchor")
        if not isinstance(anchor, datetime):
            raise ScheduleError("every_seconds braucht einen Anker (_anchor)")
        step = math.floor((after - anchor).total_seconds() / seconds) + 1
        return anchor + timedelta(seconds=step * seconds)
    if kind == "repeat":
        zone = _zone(rule)
        hour, minute = _parse_clock(rule["time"])
        weekday = rule.get("weekday")
        local = after.astimezone(zone)
        for offset in range(0, 9):                   # 8 Tage decken weekly ab
            day = (local + timedelta(days=offset)).date()
            if weekday is not None and day.weekday() != int(weekday):
                continue
            candidate = _local_instant(
                datetime(day.year, day.month, day.day, hour, minute), zone)
            if candidate > after:
                return _to_utc(candidate)
        raise ScheduleError("kein Termin in den naechsten 8 Tagen — Regel pruefen")
    raise ScheduleError("kind muss 'once' oder 'repeat' sein")


def with_anchor(rule: dict[str, Any], anchor: datetime) -> dict[str, Any]:
    """Regel + interner Anker. Der Anker ist die Einrichtungszeit und haelt eine
    feste Rate (every_seconds) stabil — dieselbe Idee wie bei DSH."""
    out = dict(rule)
    out["_anchor"] = anchor
    return out


def describe(rule: dict[str, Any]) -> str:
    """Ein Satz fuer CLI und Dashboard."""
    kind = rule.get("kind")
    if kind == "once":
        return "einmalig " + _fmt(datetime.fromisoformat(str(rule["at"])))
    if kind == "repeat" and "every_seconds" in rule:
        return f"alle {_human_seconds(int(rule['every_seconds']))}"
    if kind == "repeat":
        zone = str(rule.get("time_zone") or DEFAULT_TIME_ZONE)
        clock = str(rule.get("time"))
        if rule.get("every") == "weekly":
            day = _WEEKDAYS_DE[int(rule["weekday"])]
            return f"woechentlich, {day} {clock} ({zone})"
        return f"taeglich {clock} ({zone})"
    return "(unbekannte Regel)"


_WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def describe_short(rule: dict[str, Any]) -> str:
    """Kurzform fuer enge Spalten. Die Zeitzone steht im Detail, nicht hier."""
    kind = rule.get("kind")
    if kind == "once":
        return "einmalig"
    if kind == "repeat" and "every_seconds" in rule:
        seconds = int(rule["every_seconds"])
        if seconds % 3600 == 0:
            return f"alle {seconds // 3600} h"
        return f"alle {max(1, seconds // 60)} min"
    if kind == "repeat" and rule.get("every") == "weekly":
        return f"woechentlich {_WEEKDAYS_SHORT[int(rule['weekday'])]} {rule.get('time')}"
    if kind == "repeat":
        return f"taeglich {rule.get('time')}"
    return "-"


def _fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _human_seconds(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} Tag(en)"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} Stunde(n)"
    return f"{seconds // 60} Minute(n)"
