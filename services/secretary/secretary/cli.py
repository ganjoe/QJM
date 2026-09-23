"""Kommandozeile der Sekretaerin.

  python -m secretary.cli submit "fasse alle X-Posts von heute zusammen"
  python -m secretary.cli run
  python -m secretary.cli status <change_item_id>

  # Stehende Aufgaben (Klasse B)
  python -m secretary.cli schedule list
  python -m secretary.cli schedule add "durchsuche X nach Trump" --role cco --weekly --time 18:00
  python -m secretary.cli schedule cancel <change_item_id>
  python -m secretary.cli schedule run <change_item_id>
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from . import schedules, store
from .config import Config
from .loop import Secretary


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _local(value: Any) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "-"


def _cmd_schedule_list() -> int:
    rows = store.definitions()
    if not rows:
        print("Keine stehenden Aufgaben.")
        return 0
    print(f"{'id':<38} {'zustand':<10} {'vorkommen':>9}  {'naechster':<17} regel")
    for row in rows:
        rule = row["schedule"] or {}
        if rule.get("error"):
            text = "FEHLER: " + str(rule["error"])
        elif rule.get("kind"):
            text = schedules.describe(rule)
        else:
            text = "(keine Regel)"
        print(f"{str(row['id']):<38} {row['state']:<10} {row['run_count']:>9}  "
              f"{_local(row['next_run_at']):<17} {text}")
        print(f"{'':<38} {'':<10} {'':>9}  {row['title'][:70]}")
    return 0


def _cmd_schedule_add(args: argparse.Namespace) -> int:
    rule: dict[str, Any] = {}
    if args.at:
        rule = {"kind": "once", "at": args.at}
    elif args.in_seconds:
        at = datetime.now(timezone.utc) + timedelta(seconds=args.in_seconds)
        rule = {"kind": "once", "at": at.isoformat()}
    elif args.every_seconds:
        rule = {"kind": "repeat", "every_seconds": args.every_seconds}
    elif args.daily or args.weekly:
        rule = {"kind": "repeat", "every": "weekly" if args.weekly else "daily",
                "time": args.time, "time_zone": args.zone}
        if args.weekly:
            rule["weekday"] = args.weekday
    else:
        print("Eine Zeitangabe fehlt: --at | --in | --weekly/--daily | --every-seconds",
              file=sys.stderr)
        return 2
    if args.max_runs:
        rule["max_runs"] = args.max_runs

    try:
        canonical = schedules.parse(rule)
    except schedules.ScheduleError as exc:
        print(f"Regel unbrauchbar: {exc}", file=sys.stderr)
        return 2

    if store.role(args.role) is None:
        known = ", ".join(r["name"] for r in store.roles_catalog())
        print(f"Rolle '{args.role}' gibt es nicht. Bekannt: {known}", file=sys.stderr)
        return 2

    budget = {k: v for k, v in (("rounds", args.rounds), ("tokens", args.tokens)) if v}
    definition_id = store.create_definition(
        title=args.title or args.prompt.strip().splitlines()[0][:120],
        prompt=args.prompt, role=args.role, schedule=canonical, budget=budget,
        max_rounds=args.max_rounds,
    )
    first = schedules.first_occurrence(canonical, datetime.now(timezone.utc))
    print(f"change_item_id: {definition_id}")
    print(f"regel:          {schedules.describe(canonical)}")
    print(f"erster Termin:  {_local(first)}")
    print("Die Sekretaerin stellt sie beim naechsten Tick scharf.")
    return 0


def _cmd_roles(args: argparse.Namespace) -> int:
    if getattr(args, "roles_action", None) == "set":
        effort = None if args.effort == "default" else args.effort
        try:
            ok = store.set_role_effort(args.role, effort)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not ok:
            print(f"Rolle '{args.role}' gibt es nicht.", file=sys.stderr)
            return 1
        print(f"{args.role}: Reasoning {args.effort}")
        print("Laufende Rollen-Prozesse behalten ihren Level — er wird beim Start gesetzt.")
        print("Wirksam fuer neue (Rolle, Level)-Kombinationen.")
        return 0

    rows = store.roles_catalog()
    print(f"{'Rolle':<16} {'Reasoning':<10} {'parallel':>8}  Beschreibung")
    for row in rows:
        print(f"{row['name']:<16} {str(row.get('reasoning_effort') or 'default'):<10} "
              f"{row['max_concurrency']:>8}  {row['description'][:70]}")
    print()
    print("Setzen: secretary roles set <rolle> off|low|high|max|default")
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    if args.action == "list":
        return _cmd_schedule_list()
    if args.action == "add":
        return _cmd_schedule_add(args)
    if args.action == "cancel":
        if store.cancel_definition(args.change_item_id):
            print("Abgeschaltet: kein weiterer Termin.")
            return 0
        print("Unbekannt oder nicht mehr aktiv.", file=sys.stderr)
        return 1
    if args.action == "run":
        if store.trigger_now(args.change_item_id):
            print("Termin gezogen — das Vorkommen startet beim naechsten Tick.")
            return 0
        print("Unbekannt oder noch nicht scharfgestellt.", file=sys.stderr)
        return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secretary", description="Arbeitet den Workitem-Graphen ab.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="Boss-Prompt einreichen")
    p_submit.add_argument("prompt", help="Der Auftrag in natuerlicher Sprache")
    p_submit.add_argument("--title", default=None, help="Kurzer Titel statt der ersten Zeile")
    p_submit.add_argument("--rounds", type=int, default=None,
                          help="Budget fuer das initial-Item in Modell-Runden (Default: Policy)")
    p_submit.add_argument("--tokens", type=int, default=None,
                          help="Budget fuer das initial-Item in Tokens (Default: Policy)")
    p_submit.add_argument("--max-rounds", type=int, default=None,
                          help="Rundendeckel des LAUFS (1-10, Default 2): wie oft der Lead "
                               "nachplanen darf. Nicht zu verwechseln mit --rounds, dem "
                               "Modell-Runden-Budget des Planungs-Items.")

    sub.add_parser("run", help="Die Tick-Schleife starten")

    p_status = sub.add_parser("status", help="Einen Lauf anzeigen")
    p_status.add_argument("change_item_id")

    # ── Stehende Aufgaben ──────────────────────────────────────────────────
    p_sched = sub.add_parser("schedule", help="Stehende Aufgaben (wiederholend/verzoegert)")
    sched = p_sched.add_subparsers(dest="action", required=True)

    sched.add_parser("list", help="Alle stehenden Aufgaben mit Zustand")

    p_add = sched.add_parser("add", help="Stehende Aufgabe anlegen")
    p_add.add_argument("prompt", help="Was ausgefuehrt wird (geht an die Rolle)")
    p_add.add_argument("--role", required=True, help="Rolle, deren MCP-Werkzeuge der Lauf benutzt")
    p_add.add_argument("--title", default=None)
    p_add.add_argument("--at", default=None,
                       help="Einmalig, absoluter Zeitpunkt mit Offset (2026-09-24T18:00:00+02:00)")
    p_add.add_argument("--in", dest="in_seconds", type=int, default=None,
                       help="Einmalig, in N Sekunden")
    p_add.add_argument("--daily", action="store_true", help="Taeglich zur --time")
    p_add.add_argument("--weekly", action="store_true", help="Woechentlich am --weekday zur --time")
    p_add.add_argument("--time", default="18:00", help="Uhrzeit HH:MM (Default 18:00)")
    p_add.add_argument("--weekday", type=int, default=0, help="0=Montag ... 6=Sonntag (Default 0)")
    p_add.add_argument("--zone", default="Europe/Berlin", help="IANA-Zeitzone")
    p_add.add_argument("--every-seconds", type=int, default=None, help="Feste Rate (Minimum 300)")
    p_add.add_argument("--max-runs", type=int, default=None, help="Nach N Vorkommen beenden")
    p_add.add_argument("--rounds", type=int, default=None, help="Budget des Items in Modell-Runden")
    p_add.add_argument("--tokens", type=int, default=None, help="Budget des Items in Tokens")
    p_add.add_argument("--max-rounds", type=int, default=None,
                       help="Rundendeckel je Vorkommen (1-10, Default 2)")

    p_cancel = sched.add_parser("cancel", help="Abschalten (kein weiterer Termin)")
    p_cancel.add_argument("change_item_id")

    p_run = sched.add_parser("run", help="Sofort einmal ausfuehren")
    p_run.add_argument("change_item_id")

    # ── Rollen ─────────────────────────────────────────────────────────────
    p_roles = sub.add_parser("roles", help="Rollen ansehen / Reasoning-Level setzen")
    roles_sub = p_roles.add_subparsers(dest="roles_action")
    p_effort = roles_sub.add_parser("set", help="Reasoning-Level einer Rolle setzen")
    p_effort.add_argument("role")
    p_effort.add_argument("effort", help="off | low | high | max | default")

    args = parser.parse_args(argv)
    _setup_logging()

    if args.command == "submit":
        budget = {k: v for k, v in (("rounds", args.rounds), ("tokens", args.tokens)) if v}
        change_item_id, workitem_id = store.submit(
            args.prompt, args.title, budget, max_rounds=args.max_rounds)
        print(f"change_item_id: {change_item_id}")
        print(f"workitem_id:    {workitem_id}")
        print("Die Sekretaerin nimmt es beim naechsten Tick auf.")
        return 0

    if args.command == "run":
        Secretary(Config.from_env()).run_forever()
        return 0

    if args.command == "schedule":
        return _cmd_schedule(args)

    if args.command == "roles":
        return _cmd_roles(args)

    if args.command == "status":
        ci = store.change_item(args.change_item_id)
        if ci is None:
            print("Unbekannter Lauf.", file=sys.stderr)
            return 1
        print(f"{ci['state'].upper():8} {ci['title']}")
        print(f"         angelegt {ci['created_at']:%Y-%m-%d %H:%M}  fertig {ci['finished_at'] or '-'}")
        print()
        print(f"{'step_key':<24} {'typ':<8} {'rolle':<15} {'status':<9} "
              f"{'runde':>5} {'vers':>5} {'runden':>7} {'tokens':>9} {'sek':>5}")
        for row in store.graph(args.change_item_id):
            b = row.get("budget") or {}
            u = row.get("usage") or {}
            soll_r = b.get("rounds", "-")
            soll_t = f"{b['tokens']//1000}k" if b.get("tokens") else "-"
            ist_r = f"{u.get('rounds', 0)}/{soll_r}"
            ist_t = f"{u.get('tokens', 0)}/{soll_t}"
            print(f"{row['step_key']:<24} {row['type']:<8} {row['role']:<15} {row['status']:<9} "
                  f"{row['round']:>5} {row['attempts']:>5} {ist_r:>7} {ist_t:>9} "
                  f"{u.get('seconds', 0):>5}")
        print()
        print("  runden/tokens = Ist/Soll. Ein gerissenes Budget steht als status=failed")
        print("  mit result.reason='budget_exceeded' und result.needs.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
