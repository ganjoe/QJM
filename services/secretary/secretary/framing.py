"""Die Rahmung, mit der ein Workitem einem Agenten praesentiert wird.

Das ist die einzige Stelle, an der die Sekretaerin mit einem Agenten spricht.
Sie uebermittelt FAKTEN (welches Item, welcher Auftrag, welche Vorgaenger) —
die Verhaltensregeln stehen in der Persona der Rolle.
"""
from __future__ import annotations

import json
from typing import Any

from .config import Config


def _header(item: dict[str, Any], cfg: Config, max_rounds: int) -> list[str]:
    return [
        "[WORKITEM]",
        f"workitem_id: {item['id']}",
        f"change_item_id: {item['change_item_id']}",
        f"step_key: {item['step_key']}",
        f"type: {item['type']}",
        f"role: {item['role']}",
        f"round: {item['round']}",
        # Der Deckel gehoert zum Lauf (change_items.max_rounds), nicht zur
        # Sekretaerin: derselbe Dienst bedient Auftraege mit verschiedenen
        # Rundengrenzen.
        f"max_rounds: {max_rounds}",
    ]


def _catalog(roles: list[Any]) -> list[dict[str, Any]]:
    """Rollenzeilen normalisieren. Nimmt den Katalog (dicts) oder nackte Namen."""
    out: list[dict[str, Any]] = []
    for role in roles:
        if isinstance(role, dict):
            out.append({
                "name": str(role.get("name", "?")),
                "max_concurrency": int(role.get("max_concurrency") or 1),
                "reasoning_effort": str(role.get("reasoning_effort") or "default"),
                "description": " ".join(str(role.get("description") or "").split())
                               or "(keine Beschreibung hinterlegt)",
            })
        else:
            out.append({"name": str(role), "max_concurrency": 1,
                        "reasoning_effort": "default",
                        "description": "(keine Beschreibung hinterlegt)"})
    return sorted(out, key=lambda r: r["name"])


def build(
    item: dict[str, Any],
    predecessors: list[dict[str, Any]],
    entry_prompt: str | None,
    cfg: Config,
    roles: list[Any] | None = None,
    max_rounds: int | None = None,
) -> str:
    """Baut die erste Nachricht der Session fuer genau dieses Workitem.

    max_rounds kommt vom Lauf (change_items.max_rounds); None faellt auf den
    Dienst-Default zurueck — so bleibt der Aufruf fuer alte Laeufe gueltig.
    """
    limit = int(max_rounds or cfg.max_rounds)
    lines = _header(item, cfg, limit)
    lines += ["", "payload_json:", json.dumps(item.get("payload") or {}, ensure_ascii=False)]

    # Das Budget MUSS der Agent kennen — sonst ist die Policy eine Überraschung
    # statt eine Leitplanke. Die budget-policy.mjs liest genau diese Zeile und
    # erwartet das JSON auf DERSELBEN Zeile (einzeiliger Regex).
    budget = item.get("budget") or {}
    lines += ["", "budget_json: " + json.dumps(budget, ensure_ascii=False)]

    # Der Lead kann keine Rollen erfinden, die es nicht gibt — und er soll nach
    # Faehigkeiten waehlen, nicht nach Namen. Deshalb nennt die Rahmung jede
    # Rolle mit ihrer Datenquelle und ihrer Parallelitaet. Genau hier scheitert
    # eine Aufgabe sonst: sie landet bei einer Rolle, die die Quelle nicht hat.
    if roles and item["type"] in ("initial", "review"):
        lines += ["", "available_roles (Name [Parallelitaet, Reasoning] — Faehigkeiten, waehle danach):"]
        for role in _catalog(roles):
            lines.append(
                f"  {role['name']} [max {role['max_concurrency']} parallel, "
                f"Reasoning {role['reasoning_effort']}] — {role['description']}"
            )
        lines += [
            "",
            "Das Reasoning-Level einer Aufgabe setzt du beim Anlegen mit "
            "reasoning_effort (off|low|high|max). Ohne Angabe gilt das Level der Rolle.",
            "Nimm 'low' fuer mechanische Arbeit (zaehlen, formatieren, nachschlagen) und",
            "'high' fuer Bewertungen, Priorisierung und alles, was abwaegen muss.",
        ]

    if item["type"] in ("initial", "review") and entry_prompt:
        # Der Lead muss den Boss-Prompt kennen, um planen oder bewerten zu koennen.
        lines += ["", "boss_prompt_json:", json.dumps(entry_prompt, ensure_ascii=False)]

    if predecessors:
        lines += ["", "context_refs (Ergebnisse, die du mit workitem_results ziehen sollst):"]
        for p in predecessors:
            lines.append(f"  {p['step_key']} {p['id']} [{p['status']}]")

    if item["type"] == "review":
        lines += [
            "",
            "Du bist in der Review-Rolle. Pruefe, ob der Boss-Prompt durch die",
            f"Ergebnisse erfuellt ist. Bei round >= {limit} und Nichterfuellung",
            'schliesse mit status="failed" ab, statt eine weitere Runde zu planen.',
        ]

    lines += ["", "Schliesse dieses Workitem mit genau einem workitem_finish-Aufruf ab."]
    return "\n".join(lines)
