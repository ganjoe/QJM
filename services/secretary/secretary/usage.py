"""Ist-Verbrauch eines Laufs.

Die Zahlen kommen aus dem Lauf selbst: DeepSeekHarness.run() liefert die
Session-Events des Aktivitaetsintervalls zurueck, und jedes assistant/message
traegt data.usage (inputTokens, outputTokens, totalTokens, cacheReadTokens).

Kein Log-Parsing, kein Dateizugriff — die Werte liegen bereits vor.
"""
from __future__ import annotations

from typing import Any, Iterable


def fold(events: Iterable[dict[str, Any]] | None) -> dict[str, int]:
    """Summiert Verbrauch ueber die Events eines Laufs."""
    rounds = 0
    tokens = 0
    cache_read = 0
    output = 0
    reasoning = 0
    for event in events or []:
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data") or {}
        step = data.get("step")
        if isinstance(step, int):
            rounds = max(rounds, step)
        u = data.get("usage") or {}
        tokens += int(u.get("totalTokens") or 0)
        cache_read += int(u.get("cacheReadTokens") or 0)
        output += int(u.get("outputTokens") or 0)
        reasoning += int(u.get("reasoningTokens") or 0)
    return {
        "rounds": rounds,
        "tokens": tokens,
        "cacheReadTokens": cache_read,
        "outputTokens": output,
        "reasoningTokens": reasoning,
    }
