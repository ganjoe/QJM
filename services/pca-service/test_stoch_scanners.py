"""Focused tests for the Stochastik (10/1) signal scanners.

Run inside the PCA service container (needs duckdb + the parquet mount):

    docker exec qjm-pca-service python3 /app/test_stoch_scanners.py
"""
import numpy as np
import pandas as pd

from indicators import calculate_stochastic
from scanners import (StochX20Scanner, Stoch20To80Scanner, Stoch20To80AthScanner,
                      load_scan_frame)

FAILS = []


def check(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def brute_force(df, sc, p):
    """Independent, deliberately naive reference implementation (bar-by-bar)."""
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    close = df["close"].astype(float).to_numpy()
    n = len(df)
    k = np.full(n, np.nan)
    for i in range(n):
        if i + 1 < sc.k_period:
            continue
        win_lo = low[i - sc.k_period + 1:i + 1].min()
        win_hi = high[i - sc.k_period + 1:i + 1].max()
        span = win_hi - win_lo
        k[i] = 50.0 if span == 0 else 100.0 * (close[i] - win_lo) / span

    sig1 = np.zeros(n, dtype=bool)
    sig2 = np.zeros(n, dtype=bool)
    sig3 = np.zeros(n, dtype=bool)
    ath_high = np.full(n, np.nan)
    since_ath = np.full(n, np.nan)
    run_max = -np.inf
    ath_positions = []
    for i in range(n):
        if high[i] >= run_max:
            run_max = high[i]
            ath_positions.append(i)
        ath_high[i] = run_max
        if ath_positions and ath_positions[-1] < i:
            since_ath[i] = i - ath_positions[-1]
        if np.isnan(k[i]) or i == 0 or np.isnan(k[i - 1]):
            continue
        if k[i - 1] <= p["level"] and k[i] > p["level"]:
            sig1[i] = True
        if k[i - 1] <= p["high_level"] and k[i] > p["high_level"]:
            last_below = None
            for j in range(i - 1, -1, -1):
                if not np.isnan(k[j]) and k[j] < p["low_level"]:
                    last_below = j
                    break
            if last_below is not None and 1 <= i - last_below <= p["max_gap_bars"]:
                sig2[i] = True
                prior_ath = [a for a in ath_positions if a < i]
                if prior_ath and i >= p["min_history_bars"]:
                    last_ath = prior_ath[-1]
                    if not sig2[last_ath:i].any():
                        sig3[i] = True
    return k, sig1, sig2, sig3, ath_high, since_ath


SCRIPTS = {
    "AAPL": {},
    "MSFT": {},
    "TSLA": {},
    "NVDA": {},
}


def main():
    # --- 1. Synthetic series: verifies the exact transition semantics -------------
    # Hand-built close series so that %K(10) is fully deterministic (high=close+0.1, low=close-0.1).
    closes = [100.0] * 12 + [101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0]
    closes += [96.0] + [95.0, 94.0]          # slam down -> %K dives below 20
    closes += [112.0]                        # one-bar rip -> %K jumps above 80 AND new all-time high
    closes += [111.0, 90.0, 89.0]            # second dip below 20 (no new high)
    closes += [110.0]                        # second rip -> suppressed: a jump already happened at the ATH
    syn = pd.DataFrame({
        "timestamp": np.arange(len(closes)) * 86400 + 1700000000,
        "open": closes, "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes], "close": closes, "volume": [1_000_000] * len(closes),
    })
    sc1, sc2, sc3 = StochX20Scanner(), Stoch20To80Scanner(), Stoch20To80AthScanner()
    p_default = sc1._params(min_history_bars=0)   # the synthetic series is far shorter than 252 bars
    k_ref, sig1, sig2, sig3, _, _ = brute_force(syn, sc1, p_default)

    for sc, expected, label in ((sc1, sig1, "synthetic sig1"), (sc2, sig2, "synthetic sig2"),
                                (sc3, sig3, "synthetic sig3")):
        frame = sc.evaluate_range("SYN", syn, min_history_bars=0)
        got = frame["matched"].to_numpy()
        check(label + " matches brute force", bool((got == expected).all()),
              "got=%s expected=%s" % (list(np.flatnonzero(got)), list(np.flatnonzero(expected))))

    check("synthetic sig2 is a single-bar jump", int(sig2.sum()) == 2, int(sig2.sum()))
    check("synthetic sig3 fires only the first jump after the ATH", int(sig3.sum()) == 1, int(sig3.sum()))
    check("synthetic sig1 fires on every upward crossing of 20", int(sig1.sum()) == 2, int(sig1.sum()))

    # --- 2. Flat window convention (must match indicators.calculate_stochastic) ----
    flat = pd.DataFrame({
        "timestamp": np.arange(30) * 86400 + 1700000000,
        "open": [50.0] * 30, "high": [50.0] * 30, "low": [50.0] * 30,
        "close": [50.0] * 30, "volume": [1] * 30,
    })
    k_flat = sc1._stoch_k(flat)
    ref_flat = calculate_stochastic(flat, 10, 1, 1)["stoch_k_10_1"]
    check("flat window -> %K = 50 (same as indicators.py)",
          bool(np.allclose(k_flat.to_numpy()[9:], np.array(ref_flat[9:], dtype=float))))

    # --- 3. Real data: %K identical to the service indicator, signals causal -------
    for ticker in SCRIPTS:
        df, reason, meta = load_scan_frame(ticker, "1D", None, None, 0, False)
        if df is None:
            check("load " + ticker, False, reason)
            continue
        ref = calculate_stochastic(df, 10, 1, 1)["stoch_k_10_1"]
        ref = np.array([np.nan if v is None else v for v in ref], dtype=float)
        mine = sc1._stoch_k(df).to_numpy()
        both = ~np.isnan(ref) & ~np.isnan(mine)
        check(ticker + ": %K(10/1) matches indicators.calculate_stochastic",
              bool(both.sum() > 100 and np.allclose(ref[both], mine[both], atol=1e-4)),
              "compared=%d" % int(both.sum()))

        k_ref, s1, s2, s3, ath_high, since_ath = brute_force(df, sc1, p_default)
        for sc, expected, label in ((sc1, s1, "sig1"), (sc2, s2, "sig2"), (sc3, s3, "sig3")):
            got = sc.evaluate_range(ticker, df, **p_default)["matched"].to_numpy()
            check("%s %s: vectorized == brute force" % (ticker, label),
                  bool((got == expected).all()),
                  "vec=%d brute=%d" % (int(got.sum()), int(expected.sum())))

        # evaluate() on truncated windows must reproduce evaluate_range() exactly (causality)
        frame = sc3.evaluate_range(ticker, df, **p_default)
        mismatches = 0
        for pos in range(len(df) - 1, max(len(df) - 40, 0), -1):
            res = sc3.evaluate(ticker, df.iloc[:pos + 1].reset_index(drop=True), min_history_bars=0)
            if bool(res["matched"]) != bool(frame["matched"].iloc[pos]):
                mismatches += 1
        check(ticker + " sig3: latest-mode evaluate == range-mode flag (last 40 bars)", mismatches == 0,
              "mismatches=%d" % mismatches)
        print("      %s: bars=%d  sig1=%d sig2=%d sig3=%d  last=%s"
              % (ticker, len(df), int(s1.sum()), int(s2.sum()), int(s3.sum()),
                 df["timestamp"].iloc[-1]))

    # --- 4. Indicator parameters are FIXED at 10/1 (never the 14/3/3 defaults) ----
    for sc in (sc1, sc2, sc3):
        check("%s: k_period/slowing fixed at 10/1" % sc.name, (sc.k_period, sc.slowing) == (10, 1),
              (sc.k_period, sc.slowing))

    ticker = "AAPL"
    df, reason, meta = load_scan_frame(ticker, "1D", None, None, 0, False)
    k101 = np.array([np.nan if v is None else v for v in
                     calculate_stochastic(df, 10, 1, 1)["stoch_k_10_1"]], dtype=float)
    k1433 = np.array([np.nan if v is None else v for v in
                      calculate_stochastic(df, 14, 3, 3)["stoch_k_14_3"]], dtype=float)
    mine = sc1._stoch_k(df).to_numpy()
    both = ~np.isnan(k101) & ~np.isnan(mine)
    check("scanner %K == stoch_k_10_1 (period 10, slowing 1)",
          bool(both.sum() > 100 and np.allclose(k101[both], mine[both], atol=1e-4)),
          "compared=%d" % int(both.sum()))
    both14 = ~np.isnan(k1433) & ~np.isnan(mine)
    check("scanner %K is NOT the 14/3/3 default series",
          not bool(np.allclose(k1433[both14], mine[both14], atol=0.05)),
          "compared=%d  max_diff=%.3f" % (int(both14.sum()), float(np.nanmax(np.abs(k1433[both14] - mine[both14])))))

    plain = sc1.evaluate_range(ticker, df)["matched"].to_numpy()
    forced = sc1.evaluate_range(ticker, df, k_period=14, slowing=3, d_period=3)["matched"].to_numpy()
    check("conflicting k_period/slowing/d_period override is refused (identical result)",
          bool((plain == forced).all()))
    det = sc1.evaluate(ticker, df, k_period=14, slowing=3)["details"]
    check("refused override is visible in details",
          det.get("k_period") == 10 and det.get("slowing") == 1
          and det.get("ignored_parameter_overrides", {}).get("k_period", {}).get("used") == 10,
          det.get("ignored_parameter_overrides"))

    class SlowedScanner(StochX20Scanner):
        slowing = 3

    slowed = SlowedScanner()
    ref_slow = np.array([np.nan if v is None else v for v in
                         calculate_stochastic(df, 10, 1, 3)["stoch_k_10_3"]], dtype=float)
    mine_slow = slowed._stoch_k(df).to_numpy()
    boths = ~np.isnan(ref_slow) & ~np.isnan(mine_slow)
    check("slowing > 1 would smooth exactly like indicators.calculate_stochastic",
          bool(np.allclose(ref_slow[boths], mine_slow[boths], atol=1e-4)))

    print()
    print("FAILURES: %d %s" % (len(FAILS), FAILS if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
