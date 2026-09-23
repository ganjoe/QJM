#!/usr/bin/env python3
"""Phase 4 — Auswertung & Optimierung des Backtests (dependency-freies HTML)."""
from __future__ import annotations

import html
import numpy as np
import pandas as pd

from lab_common import OUT_DIR

MIN_N = 50


def wavg(vals, weights):
    vals = np.asarray(vals, dtype=float)
    weights = np.asarray(weights, dtype=float)
    m = np.isfinite(vals) & np.isfinite(weights) & (weights > 0)
    return float(np.sum(vals[m] * weights[m]) / np.sum(weights[m])) if m.any() else np.nan


def heat_color(v, lo=0.42, hi=0.56):
    if not np.isfinite(v):
        return "#222"
    x = min(max((v - lo) / (hi - lo), 0.0), 1.0)
    r = int(220 * (1 - x) + 40 * x)
    g = int(60 * (1 - x) + 190 * x)
    b = int(70 * (1 - x) + 90 * x)
    return f"rgb({r},{g},{b})"


def table(df, cols, fmt=None, heat_col=None):
    fmt = fmt or {}
    th = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    rows = []
    for _, r in df.iterrows():
        tds = []
        for c in cols:
            v = r[c]
            s = fmt.get(c, lambda x: x)(v)
            style = ""
            if heat_col and c == heat_col:
                style = f' style="background:{heat_color(v)}"'
            tds.append(f"<td{style}>{html.escape(str(s))}</td>")
        rows.append("<tr>" + "".join(tds) + "</tr>")
    return f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def main() -> None:
    res = pd.read_csv(OUT_DIR / "backtest_results.csv")
    r = res[res["n"] >= MIN_N].copy()
    r["wins"] = r["hit_rate"] * r["n"]
    print(f"Configs gesamt: {len(res)} | mit n>={MIN_N}: {len(r)}")

    by_series = []
    for name, g in r.groupby("series"):
        n = g["n"].sum()
        by_series.append({
            "series": name, "trades": int(n), "configs": len(g),
            "weighted_hit": g["wins"].sum() / n,
            "mean_excess": wavg(g["mean_excess"], g["n"]),
            "median_excess": wavg(g["median_excess"], g["n"]),
            "max_rel_dd": wavg(g["mean_max_rel_dd"], g["n"]),
            "min_rel": wavg(g["mean_min_rel"], g["n"]),
        })
    s = pd.DataFrame(by_series).sort_values("weighted_hit", ascending=False)
    s.to_csv(OUT_DIR / "summary_by_series.csv", index=False)

    ph = []
    for (d, f_, t), g in r.groupby(["days", "rs_from", "rs_to"]):
        n = g["n"].sum()
        ph.append({"days": d, "rs_from": f_, "rs_to": t, "trades": int(n),
                   "weighted_hit": g["wins"].sum() / n,
                   "mean_excess": wavg(g["mean_excess"], g["n"]),
                   "max_rel_dd": wavg(g["mean_max_rel_dd"], g["n"])})
    p = pd.DataFrame(ph).sort_values("weighted_hit", ascending=False)
    p.to_csv(OUT_DIR / "summary_by_phoenix.csv", index=False)

    best = r.sort_values(["hit_rate", "n"], ascending=[False, False]).head(40)

    # Pareto (n>=100): hit_rate max, max_rel_dd max (weniger negativ = besser)
    q = r[r["n"] >= 100].copy()
    pareto = []
    for _, row in q.iterrows():
        dominated = False
        for _, o in q.iterrows():
            if (o["hit_rate"] >= row["hit_rate"] and o["mean_max_rel_dd"] >= row["mean_max_rel_dd"]
                    and (o["hit_rate"] > row["hit_rate"] or o["mean_max_rel_dd"] > row["mean_max_rel_dd"])):
                dominated = True
                break
        if not dominated:
            pareto.append(row)
    par = pd.DataFrame(pareto).sort_values("hit_rate", ascending=False) if pareto else q.head(0)
    par.to_csv(OUT_DIR / "pareto.csv", index=False)

    # Heatmaps: je Top-Serie hit_rate ueber (rs_from, rs_to) bei days=20
    top_series = list(s["series"].head(4))
    heatmaps = []
    for name in top_series:
        sub = r[(r["series"] == name) & (r["days"] == 20)]
        if sub.empty:
            continue
        piv = sub.pivot_table(index="rs_from", columns="rs_to", values="hit_rate", aggfunc="mean")
        th = "".join(f"<th>to={c}</th>" for c in piv.columns)
        body = ""
        for idx, rowv in piv.iterrows():
            tds = "".join(f'<td style="background:{heat_color(v)}">{v:.3f}</td>' for v in rowv.values)
            body += f"<tr><th>from={idx}</th>{tds}</tr>"
        heatmaps.append(f"<h4>{html.escape(name)} (days=20)</h4><table class='heat'><tr><th></th>{th}</tr>{body}</table>")

    fmt = {
        "weighted_hit": lambda x: f"{x:.3f}", "hit_rate": lambda x: f"{x:.3f}",
        "mean_excess": lambda x: f"{x*100:+.1f}%", "median_excess": lambda x: f"{x*100:+.1f}%",
        "max_rel_dd": lambda x: f"{x*100:.1f}%", "min_rel": lambda x: f"{x*100:.1f}%",
        "mean_max_rel_dd": lambda x: f"{x*100:.1f}%", "mean_min_rel": lambda x: f"{x*100:.1f}%",
        "trades": lambda x: f"{int(x):,}",
    }
    css = """body{font:13px/1.5 ui-monospace,Menlo,monospace;background:#111;color:#ddd;margin:24px}
    h1,h2,h3,h4{color:#fff} table{border-collapse:collapse;margin:8px 0 20px}
    td,th{border:1px solid #333;padding:3px 7px;text-align:right} th{background:#1c1c1c;color:#9cf}
    td:first-child,th:first-child{text-align:left} .heat td,.heat th{min-width:64px}
    .note{color:#999;max-width:900px}"""

    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>RS-Lab Report</title>
<style>{css}</style></head><body>
<h1>RS-Lab — Phoenix Backtest (letzte 2 Jahre)</h1>
<p class="note">Entry = Open am Folgetag, Exit = Close nach 63 Bars, Benchmark = SPY.
Primaer: Hit-Rate (Excess &gt;= 0). Sekundaer: relativer Max-Drawdown vs. SPY.
Gefiltert auf Konfigurationen mit n &gt;= {MIN_N}. Alle Zahlen ohne Slippage/Kosten.</p>
<h2>1. RS-Varianten (trade-gewichtet)</h2>
{table(s, ['series','trades','configs','weighted_hit','mean_excess','median_excess','max_rel_dd'], fmt, 'weighted_hit')}
<h2>2. Phoenix-Parameter (ueber alle Serien, trade-gewichtet)</h2>
{table(p.head(30), ['days','rs_from','rs_to','trades','weighted_hit','mean_excess','max_rel_dd'], fmt, 'weighted_hit')}
<h2>3. Heatmaps Hit-Rate (Tages-Signalansaetze)</h2>
{''.join(heatmaps) or '<p class="note">keine</p>'}
<h2>4. Beste Einzel-Configs (n &gt;= {MIN_N})</h2>
{table(best, ['series','days','rs_from','rs_to','n','hit_rate','mean_excess','median_excess','mean_max_rel_dd','mean_min_rel'], fmt, 'hit_rate')}
<h2>5. Pareto (n &gt;= 100)</h2>
{table(par.head(30), ['series','days','rs_from','rs_to','n','hit_rate','mean_excess','mean_max_rel_dd','mean_min_rel'], fmt, 'hit_rate')}
</body></html>"""
    (OUT_DIR / "rs_lab_report.html").write_text(doc, encoding="utf-8")
    print(f"  -> {OUT_DIR / 'rs_lab_report.html'}")
    print("\nSummary by series:")
    print(s[["series", "trades", "weighted_hit", "mean_excess", "max_rel_dd"]].to_string(index=False))
    print("\nBest phoenix params (trade-gewichtet, top 10):")
    print(p.head(10)[["days", "rs_from", "rs_to", "trades", "weighted_hit", "mean_excess", "max_rel_dd"]].to_string(index=False))


if __name__ == "__main__":
    main()
