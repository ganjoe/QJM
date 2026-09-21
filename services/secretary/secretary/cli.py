"""Kommandozeile der Sekretaerin.

  python -m secretary.cli submit "fasse alle X-Posts von heute zusammen"
  python -m secretary.cli run
  python -m secretary.cli status <change_item_id>
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import store
from .config import Config
from .loop import Secretary


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


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

    sub.add_parser("run", help="Die Tick-Schleife starten")

    p_status = sub.add_parser("status", help="Einen Lauf anzeigen")
    p_status.add_argument("change_item_id")

    args = parser.parse_args(argv)
    _setup_logging()

    if args.command == "submit":
        budget = {k: v for k, v in (("rounds", args.rounds), ("tokens", args.tokens)) if v}
        change_item_id, workitem_id = store.submit(args.prompt, args.title, budget)
        print(f"change_item_id: {change_item_id}")
        print(f"workitem_id:    {workitem_id}")
        print("Die Sekretaerin nimmt es beim naechsten Tick auf.")
        return 0

    if args.command == "run":
        Secretary(Config.from_env()).run_forever()
        return 0

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
