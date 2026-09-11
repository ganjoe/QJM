"""Technical scanner framework for the QJM PCA service.

A scanner is registered once in :class:`ScannerRegistry` and executed through
``POST /api/scanner/run`` -- driven from agent-pca's ``run_technical_scanner`` MCP tool.

Universe (union, de-duplicated, ``$``-prefixed virtual tickers skipped):
  * ``tickers``    -- explicit symbols
  * ``watchlists`` -- Supabase watchlist references: bare list name, PCA service link
                      (``.../api/watchlists/<name>``), PostgREST link containing
                      ``list_name=eq.<name>`` or a ``<name>.txt`` file reference

Output modes:
  * ``latest`` -- no ``from``/``to`` given: every scanner is evaluated on the last
    available bar of each ticker, the response carries a plain true/false map (``matches``).
  * ``range``  -- ``from`` and/or ``to`` given: every bar inside the inclusive window is
    evaluated causally (no look-ahead; warm-up bars are loaded before ``from``) and the
    response carries a list of ``hits``.

Tickers without data or without enough history are never reported as ``false`` -- they are
listed in ``skipped`` with a machine readable reason.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import duckdb
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from watchlists_api import resolve_watchlist_reference

logger = logging.getLogger("pca.scanners")
router = APIRouter()


def _first_existing_dir(*candidates: Optional[str]) -> Path:
    for cand in candidates:
        if cand and Path(cand).is_dir():
            return Path(cand)
    return Path(candidates[-1]) if candidates and candidates[-1] else Path("/parquet")


# Supports both the docker path (/parquet) and the host path
PARQUET_BASE = _first_existing_dir(
    os.environ.get("PARQUET_BASE_PATH"),
    "/parquet",
    "/home/daniel/stock-data-node/data/parquet",
)

BASE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_DEFAULT_MAX_WORKERS = int(os.environ.get("SCANNER_MAX_WORKERS", "16"))
_DEFAULT_MAX_TICKERS = int(os.environ.get("SCANNER_MAX_TICKERS", "2000"))
_DEFAULT_LIMIT_HITS = int(os.environ.get("SCANNER_LIMIT_HITS", "200"))
_MAX_LIMIT_HITS = int(os.environ.get("SCANNER_MAX_HITS", "1000"))
_TOTAL_TIMEOUT_S = float(os.environ.get("SCANNER_TOTAL_TIMEOUT_S", "300"))
_UNIVERSE_ECHO_LIMIT = int(os.environ.get("SCANNER_UNIVERSE_ECHO_LIMIT", "200"))
_SKIPPED_ECHO_LIMIT = int(os.environ.get("SCANNER_SKIPPED_ECHO_LIMIT", "100"))

HIT_MODES = ("first_of_episode", "all_bars")
SKIP_REASONS = (
    "no_parquet_data",
    "timeframe_unavailable",
    "insufficient_history",
    "no_bar_in_range",
    "load_error",
    "scan_timeout",
    "universe_cap_exceeded",
)


# ─── Range bounds ─────────────────────────────────────────────────────────────

def parse_range_bound(value: Union[str, int, float, None], is_end: bool) -> Optional[int]:
    """Parses a range bound into Unix seconds (UTC). ISO date, ISO datetime or epoch (s/ms)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid range bound: {value!r}")
    if isinstance(value, (int, float)):
        ts = int(value)
        return ts // 1000 if ts > 10_000_000_000 else ts

    raw = str(value).strip()
    if raw.isdigit():
        ts = int(raw)
        return ts // 1000 if ts > 10_000_000_000 else ts

    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        suffix = "T23:59:59.999999+00:00" if is_end else "T00:00:00+00:00"
        try:
            return int(datetime.fromisoformat(raw + suffix).timestamp())
        except ValueError:
            pass

    iso = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(f"Invalid range bound '{value}': use 'YYYY-MM-DD', an ISO datetime or Unix seconds.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def ts_to_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def _clean_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper()


# ─── Request model ────────────────────────────────────────────────────────────

class ScannerRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scanners: List[str] = Field(..., description="Scanner names to run, e.g. ['madbo']")
    tickers: Optional[List[str]] = Field(default=None, description="Explicit ticker symbols")
    watchlists: Optional[List[str]] = Field(
        default=None,
        description="Watchlist references: name, '<host>/api/watchlists/<name>', "
                    "a link containing 'list_name=eq.<name>' or '<name>.txt'",
    )
    date_from: Optional[Union[str, int, float]] = Field(default=None, alias="from",
                                                        description="Inclusive range start (YYYY-MM-DD or Unix seconds)")
    date_to: Optional[Union[str, int, float]] = Field(default=None, alias="to",
                                                      description="Inclusive range end (YYYY-MM-DD or Unix seconds)")
    range_: Optional[Dict[str, Any]] = Field(default=None, alias="range",
                                             description="Alias for from/to: {'from': ..., 'to': ...}")
    timeframe: str = Field(default="1D", description="Candle timeframe (only 1D is populated)")
    hit_mode: str = Field(default="first_of_episode",
                          description="'first_of_episode' collapses consecutive matching bars, 'all_bars' keeps every bar")
    limit_hits: int = Field(default=_DEFAULT_LIMIT_HITS, description="Max hits returned (rest is truncated)")
    include_details: bool = Field(default=False, description="Attach per-ticker scores/details")
    max_tickers: int = Field(default=_DEFAULT_MAX_TICKERS, description="Universe cap")
    warmup_bars: Optional[int] = Field(default=None, description="Bars loaded before 'from' (default: scanner min_bars)")

    @property
    def has_range(self) -> bool:
        if self.date_from is not None or self.date_to is not None:
            return True
        if isinstance(self.range_, dict) and self.range_:
            return True
        return False

    def range_values(self) -> Tuple[Optional[Any], Optional[Any]]:
        start, end = self.date_from, self.date_to
        if isinstance(self.range_, dict):
            if start is None:
                start = self.range_.get("from")
            if end is None:
                end = self.range_.get("to")
        return start, end


# ─── Scanner template ─────────────────────────────────────────────────────────

class BaseScanner(ABC):
    """Scanner template: implement ``evaluate()`` (last bar) and optionally ``evaluate_range()``."""

    name: str = ""
    description: str = ""
    min_bars: int = 1
    requires_features: bool = False
    support_range: bool = True

    @abstractmethod
    def evaluate(self, ticker: str, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        """
        Evaluates the scanner condition on the LAST bar of ``df``.

        Returns {"matched": bool, "score": Optional[float], "details": dict}.
        """
        raise NotImplementedError

    def evaluate_range(self, ticker: str, df: pd.DataFrame,
                       feat_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """
        Vectorized default: causal bar-by-bar replay of ``evaluate()``.

        Returns a DataFrame indexed like ``df`` with columns ``matched`` (bool) and ``score``.
        Override for performance on long histories.
        """
        if not self.support_range:
            raise ValueError(f"Scanner '{self.name}' does not support range evaluation.")

        matched: List[bool] = []
        scores: List[Optional[float]] = []
        for i in range(len(df)):
            window = df.iloc[: i + 1]
            feat_window = feat_df.iloc[: i + 1] if isinstance(feat_df, pd.DataFrame) else None
            res = self.evaluate(ticker, window, feat_window)
            matched.append(bool(res.get("matched")))
            scores.append(res.get("score"))

        return pd.DataFrame({
            "matched": pd.Series(matched, index=df.index, dtype=bool),
            "score": pd.Series(scores, index=df.index, dtype="float64"),
        })

    def meta(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "min_bars": self.min_bars,
            "requires_features": self.requires_features,
            "support_range": self.support_range,
        }

    def data_quality_issues(self, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None,
                            positions: Optional[Sequence[int]] = None) -> int:
        """
        Number of bars inside the evaluated window that look like data defects.

        Used to warn about tickers whose price/volume history would produce synthetic matches.
        """
        return 0


# ─── Built-In Scanners ────────────────────────────────────────────────────────

class MinerviniTrendScanner(BaseScanner):
    name = "minervini_trend"
    description = ("Minervini Trend Template (score 0-6, matched at >= 5): 200 SMA trending up, "
                   "price above the 150 & 200 SMA, 50 SMA above the 150 & 200 SMA, at least 25% "
                   "above the 52-week low and within 25% of the 52-week high.")
    min_bars = 252

    def evaluate(self, ticker: str, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        if len(df) < 200:
            return {"matched": False, "score": 0, "details": {"error": "Not enough history (<200 bars)"}}

        close = df["close"]
        sma_50 = close.rolling(50).mean()
        sma_150 = close.rolling(150).mean()
        sma_200 = close.rolling(200).mean()

        c_last = float(close.iloc[-1])
        s50_last = float(sma_50.iloc[-1])
        s150_last = float(sma_150.iloc[-1])
        s200_last = float(sma_200.iloc[-1])
        s200_20d_ago = float(sma_200.iloc[-20]) if len(sma_200) >= 20 else s200_last

        # 52-week High/Low (assuming ~252 trading days)
        lookback = min(len(df), 252)
        high_52w = float(df["high"].iloc[-lookback:].max())
        low_52w = float(df["low"].iloc[-lookback:].min())

        cond1 = c_last > s150_last and c_last > s200_last
        cond2 = s150_last > s200_last
        cond3 = s200_last >= s200_20d_ago  # 200 SMA trending up
        cond4 = s50_last > s150_last and s50_last > s200_last
        cond5 = c_last >= (low_52w * 1.25)   # >= 25% above 52w low
        cond6 = c_last >= (high_52w * 0.75)  # within 25% of 52w high

        criteria = [cond1, cond2, cond3, cond4, cond5, cond6]
        score = sum(1 for c in criteria if c)
        matched = (score >= 5)

        return {
            "matched": matched,
            "score": score,
            "details": {
                "score_max": 6,
                "price": round(c_last, 2),
                "sma_50": round(s50_last, 2),
                "sma_150": round(s150_last, 2),
                "sma_200": round(s200_last, 2),
                "high_52w": round(high_52w, 2),
                "low_52w": round(low_52w, 2),
                "criteria": {
                    "price_above_150_200": cond1,
                    "sma150_above_sma200": cond2,
                    "sma200_trending_up": cond3,
                    "sma50_above_150_200": cond4,
                    "above_25pct_52w_low": cond5,
                    "within_25pct_52w_high": cond6,
                }
            }
        }

    def evaluate_range(self, ticker: str, df: pd.DataFrame,
                       feat_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        close = df["close"].astype(float)
        sma_50 = close.rolling(50).mean()
        sma_150 = close.rolling(150).mean()
        sma_200 = close.rolling(200).mean()
        sma_200_shifted = sma_200.shift(20)

        high_52w = df["high"].astype(float).rolling(252, min_periods=1).max()
        low_52w = df["low"].astype(float).rolling(252, min_periods=1).min()

        cond1 = (close > sma_150) & (close > sma_200)
        cond2 = sma_150 > sma_200
        cond3 = sma_200 >= sma_200_shifted
        cond4 = (sma_50 > sma_150) & (sma_50 > sma_200)
        cond5 = close >= (low_52w * 1.25)
        cond6 = close >= (high_52w * 0.75)

        score = (cond1.astype(int) + cond2.astype(int) + cond3.astype(int)
                 + cond4.astype(int) + cond5.astype(int) + cond6.astype(int))
        valid = pd.Series(range(1, len(df) + 1), index=df.index) >= self.min_bars
        matched = (score >= 5) & valid

        return pd.DataFrame({"matched": matched.astype(bool),
                             "score": score.where(valid, 0).astype("float64")})


class SmaCrossScanner(BaseScanner):
    name = "sma_cross"
    description = ("50/200 SMA relationship: matched while the 50 SMA holds above the 200 SMA "
                   "(golden cross active); score is the spread in percent.")
    min_bars = 200

    def evaluate(self, ticker: str, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        if len(df) < 200:
            return {"matched": False, "score": None, "details": {"error": "Not enough history (<200 bars)"}}

        close = df["close"]
        sma_50 = close.rolling(50).mean()
        sma_200 = close.rolling(200).mean()

        s50 = float(sma_50.iloc[-1])
        s200 = float(sma_200.iloc[-1])
        spread_pct = round(((s50 - s200) / s200) * 100, 2) if s200 != 0 else 0.0

        matched = (s50 > s200)
        return {
            "matched": matched,
            "score": spread_pct,
            "details": {
                "sma_50": round(s50, 2),
                "sma_200": round(s200, 2),
                "spread_pct": spread_pct,
                "golden_cross_active": matched,
            }
        }

    def evaluate_range(self, ticker: str, df: pd.DataFrame,
                       feat_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        close = df["close"].astype(float)
        sma_50 = close.rolling(50).mean()
        sma_200 = close.rolling(200).mean()
        spread = ((sma_50 - sma_200) / sma_200.where(sma_200 != 0)) * 100

        valid = pd.Series(range(1, len(df) + 1), index=df.index) >= self.min_bars
        matched = (sma_50 > sma_200) & valid

        return pd.DataFrame({"matched": matched.astype(bool),
                             "score": spread.round(2).astype("float64")})


class MadboScanner(BaseScanner):
    """MADBO -- Moving Average Dollar Volume Breakout.

    TC2000 translation (1D bars):
        #highma      = greatest(avgc(10), avgc(20), avgc(50), avgc(100), avgc(200))
        #lowma       = least(same five SMAs of close)
        #maspred     = abs(#lowma - #highma)
        #atr         = atr(1)              -> true range of the bar
        #cmaspred    = #maspred < #atr     -> moving average fan compressed
        #dvol        = volume * close
        #avgdvol     = avg(#dvol, 50)
        #cdvolspread = #dvol > 2 * #avgdvol -> dollar volume breakout
        matched      = #cmaspred AND #cdvolspread

    The extra TC2000 term ``#cdvolhigh`` (dollar volume equals its 150-bar high) is
    intentionally NOT part of the breakout condition and is therefore not computed.
    """

    name = "madbo"
    description = ("MADBO -- Moving Average Dollar Volume Breakout: the five close SMAs "
                   "(10/20/50/100/200) are compressed into a fan narrower than the bar's true range "
                   "(ATR(1)) while dollar volume (close x volume) exceeds twice its 50-bar average. "
                   "Score is the dollar-volume multiple.")
    min_bars = 200
    ma_periods: Tuple[int, ...] = (10, 20, 50, 100, 200)
    volume_multiple: float = 2.0
    avg_volume_window: int = 50

    @staticmethod
    def true_range(df: pd.DataFrame) -> pd.Series:
        """ATR(1): true range of the bar = max(high-low, |high-prev_close|, |low-prev_close|)."""
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        prev_close = df["close"].astype(float).shift(1)
        return pd.concat([(high - low).abs(),
                          (high - prev_close).abs(),
                          (low - prev_close).abs()], axis=1).max(axis=1)

    def frame(self, df: pd.DataFrame) -> pd.DataFrame:
        close = df["close"].astype(float)
        mas = pd.concat([close.rolling(p).mean() for p in self.ma_periods], axis=1)
        ma_high = mas.max(axis=1)
        ma_low = mas.min(axis=1)
        ma_spread = (ma_high - ma_low).abs()
        atr_1 = self.true_range(df)

        dollar_volume = close * df["volume"].astype(float)
        avg_dollar_volume = dollar_volume.rolling(self.avg_volume_window).mean()
        volume_multiple = dollar_volume / avg_dollar_volume.where(avg_dollar_volume != 0)

        cond_compression = ma_spread < atr_1
        cond_volume = dollar_volume > (self.volume_multiple * avg_dollar_volume)
        valid = pd.Series(range(1, len(df) + 1), index=df.index) >= self.min_bars

        # Data hygiene: bars with non-positive price/volume can satisfy the raw comparison
        # (a negative 50-bar dollar-volume average flips the breakout test) without being a
        # real breakout. Such bars are flagged as 'suspect' and never reported as hits.
        data_valid = (close > 0) & (df["volume"].astype(float) > 0) & (avg_dollar_volume > 0)

        cond_compression = cond_compression.fillna(False).astype(bool)
        cond_volume = cond_volume.fillna(False).astype(bool)
        data_valid = data_valid.fillna(False).astype(bool)

        matched = cond_compression & cond_volume & valid & data_valid
        suspect = cond_compression & cond_volume & ~data_valid

        return pd.DataFrame({
            "matched": matched.astype(bool),
            "score": volume_multiple.round(2).astype("float64"),
            "ma_high": ma_high.round(4),
            "ma_low": ma_low.round(4),
            "ma_spread": ma_spread.round(4),
            "atr_1": atr_1.round(4),
            "dollar_volume": dollar_volume.round(2),
            "avg_dollar_volume": avg_dollar_volume.round(2),
            "cond_compression": cond_compression,
            "cond_volume": cond_volume,
            "data_valid": data_valid,
            "suspect": suspect.astype(bool),
        })

    def data_quality_issues(self, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None,
                            positions: Optional[Sequence[int]] = None) -> int:
        """Counts bars that would only match because of non-positive price/volume data."""
        frame = self.frame(df)
        suspect = frame["suspect"]
        if positions is not None:
            suspect = suspect.iloc[list(positions)]
        return int(suspect.sum())

    def evaluate(self, ticker: str, df: pd.DataFrame, feat_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        if len(df) < self.min_bars:
            return {"matched": False, "score": None,
                    "details": {"error": f"Not enough history (<{self.min_bars} bars)"}}

        row = self.frame(df).iloc[-1]
        score = None if pd.isna(row["score"]) else float(row["score"])
        return {
            "matched": bool(row["matched"]),
            "score": score,
            "details": {
                "condition": "MA fan spread < ATR(1) AND dollar volume > 2x its 50-bar average",
                "ma_periods": list(self.ma_periods),
                "ma_fan_high": None if pd.isna(row["ma_high"]) else float(row["ma_high"]),
                "ma_fan_low": None if pd.isna(row["ma_low"]) else float(row["ma_low"]),
                "ma_spread": None if pd.isna(row["ma_spread"]) else float(row["ma_spread"]),
                "atr_1": None if pd.isna(row["atr_1"]) else float(row["atr_1"]),
                "dollar_volume": None if pd.isna(row["dollar_volume"]) else float(row["dollar_volume"]),
                "avg_dollar_volume_50": None if pd.isna(row["avg_dollar_volume"]) else float(row["avg_dollar_volume"]),
                "volume_multiple": score,
                "ma_fan_compressed": bool(row["cond_compression"]),
                "dollar_volume_breakout": bool(row["cond_volume"]),
                "data_quality_ok": bool(row["data_valid"]),
            }
        }

    def evaluate_range(self, ticker: str, df: pd.DataFrame,
                       feat_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        frame = self.frame(df)
        return frame[["matched", "score"]]


# ─── Scanner Registry ─────────────────────────────────────────────────────────

class ScannerRegistry:
    def __init__(self):
        self._scanners: Dict[str, BaseScanner] = {}
        # Register defaults
        self.register(MinerviniTrendScanner())
        self.register(SmaCrossScanner())
        self.register(MadboScanner())

    def register(self, scanner: BaseScanner):
        self._scanners[scanner.name.lower()] = scanner

    def get(self, name: str) -> Optional[BaseScanner]:
        return self._scanners.get((name or "").strip().lower())

    def names(self) -> List[str]:
        return [s.name for s in self._scanners.values()]

    def list_scanners(self) -> List[Dict[str, Any]]:
        return [s.meta() for s in self._scanners.values()]


registry = ScannerRegistry()


# ─── Data loading ─────────────────────────────────────────────────────────────

def resolve_parquet_path(ticker: str, timeframe: str) -> Path:
    return PARQUET_BASE / ticker.upper() / f"{timeframe.upper()}.parquet"


def load_scan_frame(ticker: str, timeframe: str, from_ts: Optional[int], to_ts: Optional[int],
                    warmup_bars: int, want_features: bool) -> Tuple[Optional[pd.DataFrame], Optional[str], Dict[str, Any]]:
    """
    Loads the scan window for a ticker.

    With ``from_ts`` set, ``warmup_bars`` bars before the range start are prepended, so every
    scanner has valid indicators from the first bar inside the window (causal, no look-ahead).
    When a scanner declares ``requires_features``, the precalculated feature columns are merged
    into the same frame (and ``feat_df`` is that frame).
    """
    meta: Dict[str, Any] = {"features_loaded": False}

    ticker_dir = PARQUET_BASE / ticker.upper()
    if not ticker_dir.is_dir():
        return None, "no_parquet_data", meta

    timeframe = (timeframe or "1D").upper()
    base_path = ticker_dir / f"{timeframe}.parquet"
    if not base_path.exists():
        return None, "timeframe_unavailable", meta

    feat_path = ticker_dir / f"{timeframe}_features.parquet"
    use_features = bool(want_features and feat_path.exists())
    target = feat_path if use_features else base_path

    db = duckdb.connect()
    try:
        schema = [r[0] for r in db.execute(f"DESCRIBE SELECT * FROM read_parquet('{target}') LIMIT 1").fetchall()]
        extra_cols = [c for c in schema if c not in set(BASE_COLUMNS) and c not in ("t", "ticker")] if use_features else []
        cols = ", ".join(BASE_COLUMNS + [f'"{c}"' for c in extra_cols])
        where_end = f" AND timestamp <= {int(to_ts)}" if to_ts is not None else ""

        if from_ts is None:
            query = f"SELECT {cols} FROM read_parquet('{target}') WHERE 1=1{where_end} ORDER BY timestamp ASC"
            df = db.execute(query).df()
        elif warmup_bars > 0:
            tail = db.execute(
                f"SELECT {cols} FROM read_parquet('{target}') WHERE timestamp < {int(from_ts)} "
                f"ORDER BY timestamp DESC LIMIT {int(warmup_bars)}"
            ).df()
            window = db.execute(
                f"SELECT {cols} FROM read_parquet('{target}') WHERE timestamp >= {int(from_ts)}{where_end} "
                f"ORDER BY timestamp ASC"
            ).df()
            df = pd.concat([tail.iloc[::-1], window], ignore_index=True)
        else:
            df = db.execute(
                f"SELECT {cols} FROM read_parquet('{target}') WHERE timestamp >= {int(from_ts)}{where_end} "
                f"ORDER BY timestamp ASC"
            ).df()
    except Exception as e:
        logger.error("Failed to load scan frame for %s: %s", ticker, e)
        return None, "load_error", meta
    finally:
        db.close()

    if df is None or df.empty:
        return None, "no_bar_in_range" if from_ts is not None else "no_parquet_data", meta

    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = df["timestamp"].astype("int64")
    meta["features_loaded"] = use_features
    meta["bars_loaded"] = int(len(df))
    return df, None, meta


# ─── Universe assembly ────────────────────────────────────────────────────────

async def build_universe(req: ScannerRunRequest, warnings: List[str]) -> Tuple[List[str], Dict[str, int]]:
    """Merges watchlist references and explicit tickers into one ordered, de-duplicated universe."""
    tickers: List[str] = []
    seen = set()
    watchlist_report: Dict[str, int] = {}

    def add(values: Sequence[Any]) -> None:
        for value in values:
            t = _clean_ticker(value)
            if not t or t.startswith("$") or t in seen:
                continue
            seen.add(t)
            tickers.append(t)

    for ref in (req.watchlists or []):
        try:
            resolved = await resolve_watchlist_reference(str(ref))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        label = resolved.get("list_name") or str(ref)
        watchlist_report[str(label)] = len(resolved.get("tickers") or [])
        if not resolved.get("tickers"):
            warnings.append(f"Watchlist reference '{ref}' resolved to 0 tickers.")
        add(resolved.get("tickers") or [])

    add(req.tickers or [])
    return tickers, watchlist_report


# ─── Per-ticker scan ──────────────────────────────────────────────────────────

def _episodes(mask: Sequence[bool], mode: str) -> List[Tuple[int, int]]:
    """Collapses a boolean bar mask into (position, bars_matched) hits."""
    episodes: List[Tuple[int, int]] = []
    if mode == "all_bars":
        return [(i, 1) for i, flag in enumerate(mask) if flag]

    start: Optional[int] = None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            episodes.append((start, i - start))
            start = None
    if start is not None:
        episodes.append((start, len(mask) - start))
    return episodes


def _scan_ticker(ticker: str, scanners: Sequence[BaseScanner], timeframe: str,
                 from_ts: Optional[int], to_ts: Optional[int], warmup_bars: int,
                 min_bars: int, want_features: bool, hit_mode: str,
                 include_details: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ticker": ticker,
        "status": "ok",
        "reason": None,
        "matches": {},
        "scores": {},
        "details": {},
        "hits": [],
        "as_of": None,
        "bars_in_range": 0,
        "warmup_short": False,
        "data_quality_bars": 0,
    }

    df, reason, meta = load_scan_frame(ticker, timeframe, from_ts, to_ts, warmup_bars, want_features)
    if df is None or df.empty:
        result.update({"status": "skipped", "reason": reason or "no_parquet_data"})
        return result

    df = df.reset_index(drop=True)
    feat_df = df if meta.get("features_loaded") else None

    ts = df["timestamp"].to_numpy()
    in_range = pd.Series(True, index=df.index)
    if from_ts is not None:
        in_range &= df["timestamp"] >= int(from_ts)
    if to_ts is not None:
        in_range &= df["timestamp"] <= int(to_ts)

    positions = [i for i, flag in enumerate(in_range.tolist()) if flag]
    result["bars_in_range"] = len(positions)

    if not positions:
        result.update({"status": "skipped", "reason": "no_bar_in_range"})
        return result
    if len(df) < min_bars:
        result.update({
            "status": "skipped",
            "reason": "insufficient_history",
            "bars_available": int(len(df)),
            "bars_required": int(min_bars),
        })
        return result

    result["as_of"] = int(ts[-1])
    if positions[0] + 1 < min_bars:
        result["warmup_short"] = True

    for sc in scanners:
        eval_positions = positions if (from_ts is not None or to_ts is not None) else [len(df) - 1]
        issues = sc.data_quality_issues(df, feat_df, eval_positions)
        if issues:
            result["data_quality_bars"] += int(issues)

        if from_ts is None and to_ts is None:
            evaluation = sc.evaluate(ticker, df, feat_df)
            result["matches"][sc.name] = bool(evaluation.get("matched", False))
            result["scores"][sc.name] = evaluation.get("score")
            if include_details:
                result["details"][sc.name] = evaluation.get("details", {})
            continue

        frame = sc.evaluate_range(ticker, df, feat_df)
        flags = frame["matched"].to_numpy().tolist()
        scores = frame["score"].to_numpy()

        # Episode collapsing happens INSIDE the requested window: a match that started before
        # 'from' and is still running is reported with the first bar inside the window.
        window_flags = [flags[p] for p in positions]
        for local_pos, bars_matched in _episodes(window_flags, hit_mode):
            pos = positions[local_pos]
            score = scores[pos]
            hit = {
                "ticker": ticker,
                "scanner": sc.name,
                "date": ts_to_date(int(ts[pos])),
                "timestamp": int(ts[pos]),
                "score": None if pd.isna(score) else round(float(score), 2),
            }
            if hit_mode == "first_of_episode" and bars_matched > 1:
                hit["bars_matched"] = int(bars_matched)
                hit["last_match_date"] = ts_to_date(int(ts[positions[local_pos + bars_matched - 1]]))
            if include_details:
                detail_res = sc.evaluate(ticker, df.iloc[: pos + 1],
                                         feat_df.iloc[: pos + 1] if feat_df is not None else None)
                hit["details"] = detail_res.get("details", {})
            result["hits"].append(hit)

    return result


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/scanner/list")
async def list_available_scanners():
    return {
        "scanners": registry.list_scanners(),
        "count": len(registry.names()),
        "hit_modes": {
            "first_of_episode": "Consecutive matching bars collapse into one hit (default).",
            "all_bars": "Every matching bar inside the range is returned as its own hit.",
        },
        "output_modes": {
            "latest": "No 'from'/'to' given: plain true/false per ticker and scanner (last available bar).",
            "range": "'from' and/or 'to' given: list of hits inside the inclusive window.",
        },
        "universe_sources": [
            "tickers: ['AAPL', 'MSFT']",
            "watchlists: ['current_positions', '.../api/watchlists/current_positions', "
            "'.../rest/v1/pca_watchlists?list_name=eq.current_positions&select=ticker', 'ai_stocks.txt']",
        ],
    }


@router.post("/scanner/run")
async def run_scanners_endpoint(req: ScannerRunRequest):
    t_start = time.perf_counter()
    warnings: List[str] = []

    # 1. Scanners
    selected: List[BaseScanner] = []
    for raw_name in (req.scanners or []):
        sc = registry.get(raw_name)
        if sc is None:
            raise HTTPException(status_code=400,
                                detail=f"Scanner '{raw_name}' not found. Available: {registry.names()}")
        if sc not in selected:
            selected.append(sc)
    if not selected:
        raise HTTPException(status_code=400, detail=f"No scanners selected. Available: {registry.names()}")

    if not req.tickers and not req.watchlists:
        raise HTTPException(status_code=400,
                            detail="Provide at least one universe source: 'tickers' and/or 'watchlists'.")

    hit_mode = (req.hit_mode or "first_of_episode").strip().lower()
    if hit_mode not in HIT_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid hit_mode '{req.hit_mode}'. Use one of {list(HIT_MODES)}.")

    if not any(sc.support_range for sc in selected) and req.has_range:
        raise HTTPException(status_code=400, detail="None of the selected scanners supports range evaluation.")

    limit_hits = max(1, min(int(req.limit_hits or _DEFAULT_LIMIT_HITS), _MAX_LIMIT_HITS))
    max_tickers = max(1, min(int(req.max_tickers or _DEFAULT_MAX_TICKERS), 20000))
    timeframe = (req.timeframe or "1D").upper()

    # 2. Time range
    raw_from, raw_to = req.range_values()
    try:
        from_ts = parse_range_bound(raw_from, is_end=False)
        to_ts = parse_range_bound(raw_to, is_end=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if from_ts is not None and to_ts is not None and from_ts > to_ts:
        raise HTTPException(status_code=400,
                            detail=f"Invalid range: 'from' ({raw_from}) is after 'to' ({raw_to}).")

    # 3. Universe
    tickers, watchlist_report = await build_universe(req, warnings)
    requested = len(tickers)
    if requested == 0:
        raise HTTPException(status_code=400, detail="Universe is empty: no tickers resolved from 'tickers'/'watchlists'.")

    skipped: List[Dict[str, Any]] = []
    if requested > max_tickers:
        dropped = tickers[max_tickers:]
        tickers = tickers[:max_tickers]
        warnings.append(f"Universe capped at {max_tickers} of {requested} tickers (raise 'max_tickers' to scan more).")
        for t in dropped[:_SKIPPED_ECHO_LIMIT]:
            skipped.append({"ticker": t, "reason": "universe_cap_exceeded"})
        if len(dropped) > _SKIPPED_ECHO_LIMIT:
            skipped.append({"ticker": f"(+{len(dropped) - _SKIPPED_ECHO_LIMIT} more)",
                            "reason": "universe_cap_exceeded"})

    # 4. Scan
    min_bars = max(sc.min_bars for sc in selected)
    warmup_bars = int(req.warmup_bars) if req.warmup_bars is not None else min_bars
    warmup_bars = max(0, min(warmup_bars, 5000))
    want_features = any(sc.requires_features for sc in selected)
    deadline = t_start + _TOTAL_TIMEOUT_S

    logger.info("Scanner run: scanners=%s tickers=%d timeframe=%s range=%s..%s hit_mode=%s",
                [sc.name for sc in selected], len(tickers), timeframe, from_ts, to_ts, hit_mode)

    ticker_results: List[Dict[str, Any]] = []
    max_workers = max(1, min(_DEFAULT_MAX_WORKERS, len(tickers)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_scan_ticker, t, selected, timeframe, from_ts, to_ts,
                            warmup_bars, min_bars, want_features, hit_mode, req.include_details): t
            for t in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            if time.perf_counter() > deadline:
                break
            try:
                ticker_results.append(future.result())
            except Exception as e:
                logger.error("Scan failed for %s: %s", ticker, e)
                ticker_results.append({"ticker": ticker, "status": "skipped", "reason": "load_error",
                                       "error": str(e), "matches": {}, "scores": {}, "details": {},
                                       "hits": [], "as_of": None, "bars_in_range": 0})
        pending = [t for f, t in futures.items() if not f.done()]
    for t in pending:
        ticker_results.append({"ticker": t, "status": "skipped", "reason": "scan_timeout",
                               "matches": {}, "scores": {}, "details": {}, "hits": [],
                               "as_of": None, "bars_in_range": 0})
    if pending:
        warnings.append(f"Scan deadline of {_TOTAL_TIMEOUT_S}s reached: {len(pending)} tickers skipped (scan_timeout).")

    ticker_results.sort(key=lambda r: r["ticker"])

    for res in ticker_results:
        if res.get("status") == "skipped":
            entry = {"ticker": res["ticker"], "reason": res.get("reason") or "load_error"}
            if res.get("bars_available") is not None:
                entry["bars_available"] = res["bars_available"]
            if res.get("error"):
                entry["error"] = res["error"]
            skipped.append(entry)

    warmup_short = [r["ticker"] for r in ticker_results if r.get("warmup_short")]
    if warmup_short:
        warnings.append(
            f"{len(warmup_short)} ticker(s) had less warm-up history than {min_bars} bars before the range start; "
            f"bars before the histogram was valid cannot match (e.g. {', '.join(warmup_short[:5])})."
        )

    data_quality = [(r["ticker"], r["data_quality_bars"]) for r in ticker_results if r.get("data_quality_bars")]
    if data_quality:
        examples = ", ".join(f"{t} ({n} bar(s))" for t, n in data_quality[:5])
        warnings.append(
            f"{len(data_quality)} ticker(s) contain bars that satisfy the raw scanner condition only because of "
            f"non-positive price/volume data ({examples}); those bars are never reported as hits."
        )

    elapsed = round(time.perf_counter() - t_start, 3)
    scanned = sum(1 for r in ticker_results if r.get("status") == "ok")
    universe_block = {
        "requested": requested,
        "resolved": len(tickers),
        "scanned": scanned,
        "watchlists": watchlist_report,
    }
    if len(tickers) <= _UNIVERSE_ECHO_LIMIT:
        universe_block["tickers"] = tickers

    skipped_echo = skipped[:_SKIPPED_ECHO_LIMIT]
    if len(skipped) > _SKIPPED_ECHO_LIMIT:
        skipped_echo.append({"ticker": f"(+{len(skipped) - _SKIPPED_ECHO_LIMIT} more)", "reason": "truncated"})

    response: Dict[str, Any] = {
        "status": "ok",
        "mode": "range" if req.has_range else "latest",
        "scanners": [sc.name for sc in selected],
        "timeframe": timeframe,
        "universe": universe_block,
        "hits": [],
        "skipped": skipped_echo,
        "warnings": warnings,
        "timing": {"elapsed_seconds": elapsed, "workers": max_workers},
    }

    if not req.has_range:
        # Latest mode: plain true/false per ticker and scanner, evaluated on the last bar only.
        matches: Dict[str, Dict[str, bool]] = {}
        as_of_ts: Optional[int] = None
        for res in ticker_results:
            if res.get("status") != "ok":
                continue
            matches[res["ticker"]] = {sc.name: bool(res["matches"].get(sc.name, False)) for sc in selected}
            if res.get("as_of"):
                as_of_ts = res["as_of"] if as_of_ts is None else max(as_of_ts, res["as_of"])
        response["matches"] = matches
        response["as_of"] = ts_to_date(as_of_ts) if as_of_ts else None
        response["true_count"] = {sc.name: sum(1 for m in matches.values() if m.get(sc.name)) for sc in selected}
        if req.include_details:
            response["scores"] = {r["ticker"]: {sc.name: r["scores"].get(sc.name) for sc in selected}
                                  for r in ticker_results if r.get("status") == "ok"}
            response["details"] = {r["ticker"]: r["details"] for r in ticker_results
                                   if r.get("status") == "ok" and r.get("details")}
        return response

    # Range mode: list of hits inside [from, to].
    all_hits: List[Dict[str, Any]] = []
    for res in ticker_results:
        if res.get("status") == "ok":
            all_hits.extend(res.get("hits") or [])

    all_hits.sort(key=lambda h: (-h["timestamp"], h["ticker"], h["scanner"]))
    hits_total = len(all_hits)
    hits = all_hits[:limit_hits]

    by_scanner: Dict[str, int] = {}
    by_ticker: Dict[str, int] = {}
    for hit in all_hits:
        by_scanner[hit["scanner"]] = by_scanner.get(hit["scanner"], 0) + 1
        by_ticker[hit["ticker"]] = by_ticker.get(hit["ticker"], 0) + 1

    bars = [r["bars_in_range"] for r in ticker_results if r.get("status") == "ok"]
    response["range"] = {
        "from": ts_to_date(from_ts) if from_ts is not None else None,
        "from_ts": from_ts,
        "to": ts_to_date(to_ts) if to_ts is not None else None,
        "to_ts": to_ts,
        "hit_mode": hit_mode,
        "bars_in_window_max": max(bars) if bars else 0,
    }
    response["hits"] = hits
    response["summary"] = {
        "hits_total": hits_total,
        "hits_returned": len(hits),
        "truncated": hits_total > len(hits),
        "tickers_with_hits": len(by_ticker),
        "by_scanner": by_scanner,
        "by_ticker": by_ticker,
    }
    if hits_total > len(hits):
        response["warnings"].append(
            f"Output truncated: {hits_total} hits found, first {len(hits)} returned (raise 'limit_hits' up to {_MAX_LIMIT_HITS})."
        )
    return response
