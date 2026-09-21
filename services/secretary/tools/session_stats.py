#!/usr/bin/env python3
"""session_stats.py — Warum dauert ein Lauf so lange?

Liest DSH-Session-Logs (session.v3.jsonl.zstd) und schluesselt die Zeit auf:
Modell-Runden gegen Tool-Ausfuehrung, plus die haeufigsten und die langsamsten
Schritte.

    python session_stats.py                    # alle Sessions des Workspace
    python session_stats.py 89e1904c 07e2f901  # nur diese (Praefix genuegt)
"""
from __future__ import annotations

import collections
import glob
import json
import os
import subprocess
import sys

SESSIONS_DIR = os.path.expanduser("~/.dsh/sessions/--home-daniel-QJM--")


def load(session_dir: str) -> list[dict]:
    log = os.path.join(session_dir, "session.v3.jsonl.zstd")
    if not os.path.exists(log):
        return []
    raw = subprocess.run(["zstdcat", log], capture_output=True, check=True).stdout
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def analyze(events: list[dict]) -> dict:
    times = [e["time"] for e in events if e.get("time")]
    if len(times) < 2:
        return {}
    total = (times[-1] - times[0]) / 1000.0

    calls: collections.Counter[str] = collections.Counter()
    open_calls: dict[str, int] = {}
    tool_seconds = 0.0
    per_tool_seconds: collections.Counter[str] = collections.Counter()
    steps: list[tuple[int, float]] = []
    open_step: int | None = None
    step_start = 0

    for e in events:
        kind, t, d = e.get("type"), e.get("time"), e.get("data") or {}
        if kind == "step/start":
            open_step, step_start = d.get("step"), t
        elif kind == "step/end" and open_step is not None and t:
            steps.append((open_step, (t - step_start) / 1000.0))
            open_step = None
        elif kind == "tool/call":
            calls[d.get("name") or "?"] += 1
            open_calls[d.get("callId")] = t
        elif kind == "tool/result":
            cid = (d.get("message") or {}).get("source", {}).get("callId")
            start = open_calls.pop(cid, None)
            if start and t:
                delta = (t - start) / 1000.0
                tool_seconds += delta
                name = next((k for k, v in calls.items() if False), "?")
                per_tool_seconds[name] += 0  # Name wird unten aus calls nachgezogen
    # Tool-Namen je callId nachtraeglich zuordnen (Reihenfolge der Events ist stabil)
    name_of: dict[str, str] = {}
    for e in events:
        if e.get("type") == "tool/call":
            name_of[e["data"].get("callId")] = e["data"].get("name") or "?"
    open_calls.clear()
    tool_seconds = 0.0
    per_tool_seconds.clear()
    for e in events:
        kind, t, d = e.get("type"), e.get("time"), e.get("data") or {}
        if kind == "tool/call":
            open_calls[d.get("callId")] = t
        elif kind == "tool/result":
            cid = (d.get("message") or {}).get("source", {}).get("callId")
            start = open_calls.pop(cid, None)
            if start and t:
                delta = (t - start) / 1000.0
                tool_seconds += delta
                per_tool_seconds[name_of.get(cid, "?")] += delta

    return {
        "total": total,
        "steps": len(steps),
        "calls": sum(calls.values()),
        "tool_seconds": tool_seconds,
        "model_seconds": total - tool_seconds,
        "by_tool": calls,
        "tool_time": per_tool_seconds,
        "slowest": sorted(steps, key=lambda s: -s[1])[:5],
        "shell": sum(calls[n] for n in ("bash", "edit", "read", "write", "glob", "grep")),
        "mcp": sum(v for k, v in calls.items() if k.startswith("mcp__")),
    }


def short(name: str) -> str:
    return name.replace("mcp__openbrain-", "").replace("__", "/")


def report(label: str, s: dict) -> None:
    if not s:
        print(f"{label}: keine Daten")
        return
    print(f"── {label}")
    print(f"   Gesamt {s['total']:.0f}s  |  {s['steps']} Modell-Runden  |  {s['calls']} Tool-Aufrufe")
    print(f"   Modell {s['model_seconds']:.0f}s ({100*s['model_seconds']/s['total']:.0f}%)"
          f"  |  Tools {s['tool_seconds']:.0f}s ({100*s['tool_seconds']/s['total']:.0f}%)"
          f"  |  {s['total']/max(1,s['steps']):.1f}s pro Runde")
    print(f"   davon shell/fs: {s['shell']}   mcp: {s['mcp']}")
    top = ", ".join(f"{short(k)}={v}" for k, v in s["by_tool"].most_common(6))
    print(f"   Tools: {top}")
    slow = ", ".join(f"step {n} ({d:.1f}s)" for n, d in s["slowest"])
    print(f"   langsamste Schritte: {slow}")
    print()


def main(argv: list[str]) -> int:
    wanted = argv[1:]
    dirs = sorted(glob.glob(os.path.join(SESSIONS_DIR, "*")),
                  key=os.path.getmtime, reverse=True)
    if wanted:
        dirs = [d for d in dirs if any(os.path.basename(d).startswith(w) for w in wanted)]
    else:
        dirs = dirs[:6]

    for d in dirs:
        events = load(d)
        if not events:
            continue
        first = next((e for e in events if e.get("type") == "user/message"), None)
        text = ""
        if first:
            for block in first.get("data", {}).get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")[:60].replace("\n", " ")
                    break
        report(f"{os.path.basename(d)[:8]}  {text}", analyze(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
