"""Die Zeitregel als Satz — und ihre Pruefung.

Es gibt genau EINE Stelle, die Regeln kennt: services/secretary/secretary/
schedules.py. Der Server bindet diese Datei ein (im Container als Volume
gemountet), statt eine zweite Formulierung zu pflegen.

Faellt der Import aus, bleibt der Server benutzbar: dann wird die Regel als
JSON gezeigt und NICHT geprueft — die Sekretaerin lehnt sie beim Scharfstellen
ab und die Definition steht als failed mit schedule.error. Das ist der
schlechtere Weg, aber kein stiller.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_parse = _describe = _describe_short = None


def load(path: Path) -> bool:
    """Bindet schedules.py ein. Gibt zurueck, ob es geklappt hat."""
    global _parse, _describe, _describe_short
    try:
        if not path.exists():
            raise FileNotFoundError(path)
        sys.path.insert(0, str(path.parent))
        import importlib.util

        spec = importlib.util.spec_from_file_location("wiq_schedules_source", path)
        if spec is None or spec.loader is None:
            raise ImportError("kein Loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _parse = module.parse
        _describe = module.describe
        _describe_short = module.describe_short
        return True
    except Exception:  # noqa: BLE001 - der Server darf daran nicht scheitern
        return False


def available() -> bool:
    return _parse is not None


class RuleError(ValueError):
    """Die Regel ist unbrauchbar. Wird dem Client als 400 gemeldet."""


def parse(rule: Any) -> dict[str, Any]:
    if _parse is None:
        raise RuleError("Regelpruefung nicht verfuegbar (schedules.py nicht eingebunden)")
    try:
        return dict(_parse(rule))
    except Exception as exc:  # noqa: BLE001 - ScheduleError ist ein ValueError
        raise RuleError(str(exc)) from exc


def describe(rule: Any) -> str:
    if not isinstance(rule, dict) or not rule.get("kind"):
        return "-"
    if rule.get("error"):
        return "FEHLER: " + str(rule["error"])
    if _describe is not None:
        try:
            return str(_describe(rule))
        except Exception:  # noqa: BLE001
            pass
    return json.dumps(rule, ensure_ascii=False)


def describe_short(rule: Any) -> str:
    if not isinstance(rule, dict) or not rule.get("kind"):
        return "-"
    if rule.get("error"):
        return "FEHLER"
    if _describe_short is not None:
        try:
            return str(_describe_short(rule))
        except Exception:  # noqa: BLE001
            pass
    return json.dumps(rule, ensure_ascii=False)
