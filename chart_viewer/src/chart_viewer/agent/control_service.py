"""Agent-side control service backing the viewer control panel.

The desktop viewer sends `control.request` envelopes (symbol search, watchlist
CRUD, chart presets) over the existing WebSocket protocol; this service performs
the database / PCA-Service work on the agent host and answers with
`control.response`. The viewer never touches Supabase credentials.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from chart_viewer import chart_spec
from chart_viewer.agent.supabase import SupabaseError, extract_url_error_msg, supabase_count, supabase_get
from chart_viewer.models.envelope import MessageKind, make_envelope
from chart_viewer.models.validation import sanitize_pane_scales

logger = logging.getLogger(__name__)

# Ticker symbols: letters, digits, dot, dash, caret (indices). Everything else is dropped.
_TICKER_SANITIZE_RE = re.compile(r"[^A-Za-z0-9.^-]")
_LIST_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.\- ]")
_MAX_QUERY_LEN = 12
_MAX_LIST_NAME_LEN = 64
_MASTER_UNIVERSE_NAMES = {"all", "master", "universe", "all.txt"}


def sanitize_query(raw: Any) -> str:
    """Normalize a typed search query (upper case, no wildcards/control chars)."""
    text = str(raw or "").strip()
    text = _TICKER_SANITIZE_RE.sub("", text)[:_MAX_QUERY_LEN]
    return text.upper()


def sanitize_ticker(raw: Any) -> str:
    """Normalize a ticker for watchlist mutations."""
    return _TICKER_SANITIZE_RE.sub("", str(raw or "").strip()).upper()[:24]


def clean_list_name(raw: Any) -> str:
    return _LIST_NAME_SANITIZE_RE.sub("", str(raw or "").strip())[:_MAX_LIST_NAME_LEN]


def is_master_universe(name: Optional[str]) -> bool:
    return str(name or "").strip().lower() in _MASTER_UNIVERSE_NAMES


class ControlError(Exception):
    """Control op failed with a well-defined error code for the panel."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ControlConfig:
    """Environment-driven configuration of the control service."""

    pca_url: str = "http://127.0.0.1:8794"
    stock_data_url: str = "http://host.docker.internal:8002"
    control_api_url: str = "http://127.0.0.1:8766"
    http_timeout_s: float = 8.0
    download_timeout_s: float = 5.0
    search_cache_ttl_s: float = 30.0
    search_cache_max_entries: int = 64
    search_limit: int = 20
    search_limit_max: int = 50
    max_workers: int = 2

    @classmethod
    def from_env(cls) -> "ControlConfig":
        def _f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "") or default)
            except (TypeError, ValueError):
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, "") or default)
            except (TypeError, ValueError):
                return default

        return cls(
            pca_url=os.environ.get("CV_CONTROL_PCA_URL") or os.environ.get("PCA_SERVICE_URL", "http://127.0.0.1:8794"),
            stock_data_url=(
                os.environ.get("CV_CONTROL_STOCK_DATA_URL")
                or os.environ.get("STOCK_DATA_NODE_URL", "http://host.docker.internal:8002")
            ),
            control_api_url=os.environ.get("CV_CONTROL_API_URL", "http://127.0.0.1:8766"),
            http_timeout_s=_f("CV_CONTROL_HTTP_TIMEOUT_SEC", 8.0),
            download_timeout_s=_f("CV_CONTROL_DOWNLOAD_TIMEOUT_SEC", 5.0),
            search_cache_ttl_s=_f("CV_CONTROL_SEARCH_CACHE_TTL_SEC", 30.0),
            search_cache_max_entries=_i("CV_CONTROL_SEARCH_CACHE_MAX", 64),
            search_limit=_i("CV_CONTROL_SEARCH_LIMIT", 20),
            search_limit_max=_i("CV_CONTROL_SEARCH_LIMIT_MAX", 50),
            max_workers=max(1, _i("CV_CONTROL_WORKERS", 2)),
        )


class ControlService:
    """Executes control-panel operations for a :class:`ChartAgent`."""

    def __init__(self, agent: Any, config: Optional[ControlConfig] = None) -> None:
        self.agent = agent
        self.config = config or ControlConfig.from_env()
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.max_workers, thread_name_prefix="cv-control"
        )
        # Suche: eigener einreihiger Executor. Damit bleiben die Antworten in der
        # Reihenfolge der Anfragen (der Client schickt ohnehin nur eine Suche
        # gleichzeitig), ohne dass der WebSocket-Empfangs-Thread auf HTTP wartet.
        self._search_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cv-search")
        self._cache: Dict[Tuple[str, int], Tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        # Injectable for tests
        self._supabase_get: Callable[..., Any] = supabase_get
        self._supabase_count: Callable[..., int] = supabase_count

        self._ops: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "search_symbols": self._op_search_symbols,
            "list_watchlists": self._op_list_watchlists,
            "get_watchlist": self._op_get_watchlist,
            "mutate_watchlist": self._op_mutate_watchlist,
            "list_presets": self._op_list_presets,
            "apply_preset": self._op_apply_preset,
            "list_panes": self._op_list_panes,
            "list_charts": self._op_list_charts,
            "get_chart_state": self._op_get_chart_state,
            "compose_chart": self._op_compose_chart,
            "apply_chart": self._op_apply_chart,
            "save_chart": self._op_save_chart,
            "diff_chart": self._op_diff_chart,
            "list_topbar_metrics": self._op_list_topbar_metrics,
            "list_windows": self._op_list_windows,
        }

    # ── dispatch ───────────────────────────────────────────────────────────

    def execute(self, op: str, params: Any) -> Dict[str, Any]:
        """Synchroner Fassadeneinstieg (HTTP/MCP) mit denselben Handlern wie handle().

        Damit gibt es genau eine Implementierung der Chart-Lifecycle-Logik: der
        WebSocket-Pfad des Control Panels und der /api/command-Pfad des MCP
        teilen sie sich.
        """
        clean_params = params if isinstance(params, dict) else {}
        handler = self._ops.get(op)
        if handler is None:
            return {
                "ok": False,
                "error": {"code": "invalid_request", "message": f"Unknown control op '{op}'"},
            }
        try:
            data = handler(clean_params)
        except Exception as exc:  # noqa: BLE001 - bewusst dieselbe Abbildung wie im WS-Pfad
            code, message = self._error_mapping(exc, op)
            return {"ok": False, "error": {"code": code, "message": message}}
        return {"ok": True, "data": data}

    @staticmethod
    def _error_mapping(exc: BaseException, op: str) -> Tuple[str, str]:
        """Exception -> (code, message). Gemeinsam fuer WS- und HTTP-Pfad."""
        if isinstance(exc, ControlError):
            return exc.code, str(exc)
        if isinstance(exc, SupabaseError):
            return "backend_unreachable", str(exc)
        if isinstance(exc, TimeoutError):
            return "timeout", str(exc) or "request timed out"
        if isinstance(exc, urllib.error.URLError):
            return "backend_unreachable", extract_url_error_msg(exc)
        if isinstance(exc, OSError):
            return "backend_unreachable", str(exc)
        logger.exception("control op '%s' failed", op)
        return "internal", str(exc)

    def handle(self, request_id: str, op: str, params: Any) -> None:
        """Handle one `control.request` - niemals blockierend auf dem Aufrufer.

        Der Aufrufer ist der WebSocket-Empfangs-Thread. Wartet der auf HTTP (die
        Suche lief hier frueher inline), nimmt er keine Nachrichten mehr ab: der
        Puffer laeuft voll, Keepalive-Pings bleiben unbeantwortet und die
        Verbindung stirbt - genau das erzeugte "keine Antwort vom Agent" und den
        Nachlauf der Klicks.
        """
        clean_params = params if isinstance(params, dict) else {}
        handler = self._ops.get(op)
        if handler is None:
            self._respond_error(request_id, op, "invalid_request", f"Unknown control op '{op}'")
            return
        executor = self._search_executor if op == "search_symbols" else self._executor
        executor.submit(self._run_handler, request_id, op, handler, clean_params)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
        self._search_executor.shutdown(wait=False)

    def _run_handler(
        self, request_id: str, op: str, handler: Callable[[Dict[str, Any]], Any], params: Dict[str, Any]
    ) -> None:
        started = time.time()
        try:
            data = handler(params)
        except Exception as e:  # noqa: BLE001 - Abbildung passiert in _error_mapping
            code, message = self._error_mapping(e, op)
            self._respond_error(request_id, op, code, message)
        else:
            self._respond_ok(request_id, op, data, started)

    def _respond_ok(self, request_id: str, op: str, data: Any, started: float) -> None:
        self._send(
            {
                "request_id": request_id,
                "op": op,
                "ok": True,
                "data": data,
                "error": None,
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        )

    def _respond_error(self, request_id: str, op: str, code: str, message: str) -> None:
        self._send(
            {
                "request_id": request_id,
                "op": op,
                "ok": False,
                "data": None,
                "error": {"code": code, "message": message},
            }
        )

    def _send(self, payload: Dict[str, Any]) -> None:
        env = make_envelope("control.response", payload=payload, kind=MessageKind.EVENT)
        try:
            self.agent.transport.send_command(env)
        except Exception as e:
            logger.warning("Could not send control.response (%s): %s", payload.get("op"), e)

    # ── symbol search ──────────────────────────────────────────────────────

    def _op_search_symbols(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = sanitize_query(params.get("query"))
        raw_limit = params.get("limit")
        try:
            limit = int(raw_limit) if raw_limit else self.config.search_limit
        except (TypeError, ValueError):
            limit = self.config.search_limit
        limit = max(1, min(limit, self.config.search_limit_max))

        if not query:
            return {"results": [], "match_count": 0, "source": "empty"}

        cache_key = (query, limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        results = self._query_universe(query, limit, contains=False)
        if not results and len(query) >= 2:
            results = self._query_universe(query, limit, contains=True)

        results.sort(key=lambda r: (0 if r["ticker"] == query else 1, r["ticker"]))
        data = {"results": results, "match_count": len(results), "source": "db"}
        self._cache_put(cache_key, data)
        return data

    def _query_universe(self, query: str, limit: int, contains: bool) -> list:
        pattern = f"%{query}%" if contains else f"{query}%"
        path = (
            "/cda_master_universe?select=ticker,type,has_parquet"
            f"&ticker=ilike.{urllib.parse.quote(pattern)}&order=ticker.asc&limit={limit}"
        )
        rows = self._supabase_get(path) or []
        results = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if not ticker:
                continue
            results.append(
                {
                    "ticker": ticker,
                    "type": str(row.get("type") or ""),
                    "has_parquet": bool(row.get("has_parquet")),
                }
            )
        return results

    def _cache_get(self, key: Tuple[str, int]) -> Optional[Dict[str, Any]]:
        with self._cache_lock:
            entry = self._cache.get(key)
        if not entry:
            return None
        ts, data = entry
        if time.time() - ts > self.config.search_cache_ttl_s:
            return None
        return data

    def _cache_put(self, key: Tuple[str, int], data: Dict[str, Any]) -> None:
        with self._cache_lock:
            if len(self._cache) >= self.config.search_cache_max_entries:
                oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
                self._cache.pop(oldest, None)
            self._cache[key] = (time.time(), data)

    def invalidate_search_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    # ── watchlists ─────────────────────────────────────────────────────────

    def _op_list_watchlists(self, params: Dict[str, Any]) -> Dict[str, Any]:
        res = self._pca_json("GET", "/api/watchlists")
        names = res.get("watchlists") if isinstance(res, dict) else None
        names = [str(n) for n in (names or []) if str(n).strip()]
        user_lists = sorted({n for n in names if not is_master_universe(n)})
        watchlists = [{"name": "all", "editable": False}]
        watchlists += [{"name": n, "editable": True} for n in user_lists]
        return {"watchlists": watchlists}

    def _op_get_watchlist(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = clean_list_name(params.get("list_name"))
        if not name:
            raise ControlError("invalid_request", "list_name is required")

        if is_master_universe(name):
            try:
                count = int(self._supabase_count("/cda_master_universe?select=ticker&has_parquet=eq.true") or 0)
            except Exception as e:
                logger.warning("Could not count master universe: %s", e)
                count = 0
            return {
                "list_name": "all",
                "tickers": [],
                "count": count,
                "editable": False,
                "truncated": True,
            }

        res = self._pca_json("GET", f"/api/watchlists/{urllib.parse.quote(name)}")
        rows = res.get("tickers") if isinstance(res, dict) else None
        tickers = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if not ticker:
                continue
            position = row.get("position")
            tickers.append({"ticker": ticker, "position": position if isinstance(position, int) else 0})
        tickers.sort(key=lambda r: r["position"])
        return {
            "list_name": name,
            "tickers": tickers,
            "count": len(tickers),
            "editable": True,
            "truncated": False,
        }

    def _op_mutate_watchlist(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Ticker- und Listen-Operationen der Panel-Buttons.

        Actions: 'add'/'remove' (einzelner Ticker), 'copy'/'move' (Ticker in eine
        andere Liste), 'copy_list' (neue Liste aus einer bestehenden) und 'delete'
        (ganze Liste). Die Master-Liste 'all' ist in jeder Rolle geschützt.
        """
        action = str(params.get("action") or "").strip().lower()
        name = clean_list_name(params.get("list_name"))
        target = clean_list_name(params.get("target_list"))
        ticker = sanitize_ticker(params.get("ticker"))

        if action not in ("add", "remove", "copy", "move", "copy_list", "delete"):
            raise ControlError(
                "invalid_request",
                "action must be 'add', 'remove', 'copy', 'move', 'copy_list' or 'delete'",
            )
        if not name:
            raise ControlError("invalid_request", "list_name is required")
        if is_master_universe(name):
            raise ControlError("protected_list", "Die Master-Watchlist 'all' ist schreibgeschützt.")
        if action in ("add", "remove", "copy", "move") and not ticker:
            raise ControlError("invalid_request", "ticker is required")
        if action in ("copy", "move", "copy_list"):
            if not target:
                raise ControlError("invalid_request", "target_list is required")
            if is_master_universe(target):
                raise ControlError("protected_list", "Die Master-Watchlist 'all' ist schreibgeschützt.")
            if target == name:
                raise ControlError("invalid_request", "Quelle und Ziel sind identisch")

        if action == "delete":
            self._pca_json("DELETE", f"/api/watchlists/{urllib.parse.quote(name)}")
            self._drop_open_watchlist(name)
            return {"status": "deleted", "list_name": name}

        if action == "copy_list":
            source = self._op_get_watchlist({"list_name": name})
            tickers = [
                str(t["ticker"]) for t in source.get("tickers") or [] if t.get("ticker")
            ]
            if not tickers:
                raise ControlError("empty_source", f"Watchlist '{name}' enthält keine Ticker")
            self._pca_json(
                "POST",
                "/api/watchlists/batch",
                {"list_name": target, "tickers": tickers, "replace": True},
            )
            self._refresh_open_watchlist(target)
            return {
                "status": "copied",
                "list_name": name,
                "target_list": target,
                "count": len(tickers),
                "target_count": len(tickers),
            }

        if action in ("copy", "move"):
            res = self._pca_json(
                "POST", "/api/watchlists", {"list_name": target, "ticker": ticker, "position": 0}
            )
            status = str(res.get("status") or "added") if isinstance(res, dict) else "added"
            if status not in ("added", "already_exists"):
                status = "added"
            self._trigger_download([ticker])
            if action == "move":
                self._pca_json(
                    "DELETE",
                    f"/api/watchlists/{urllib.parse.quote(name)}/{urllib.parse.quote(ticker)}",
                )
                self._refresh_open_watchlist(name)
                status = "moved"
            self._refresh_open_watchlist(target)
            return {
                "status": status,
                "list_name": name,
                "target_list": target,
                "ticker": ticker,
                "count": self._watchlist_count(name),
                "target_count": self._watchlist_count(target),
            }

        if action == "add":
            res = self._pca_json("POST", "/api/watchlists", {"list_name": name, "ticker": ticker, "position": 0})
            status = str(res.get("status") or "added") if isinstance(res, dict) else "added"
            if status not in ("added", "already_exists"):
                status = "added"
            self._trigger_download([ticker])
        else:  # remove
            self._pca_json("DELETE", f"/api/watchlists/{urllib.parse.quote(name)}/{urllib.parse.quote(ticker)}")
            status = "removed"

        self._refresh_open_watchlist(name)
        return {
            "status": status,
            "list_name": name,
            "ticker": ticker,
            "count": self._watchlist_count(name),
        }

    def _watchlist_count(self, list_name: str) -> Optional[int]:
        """Ticker count of a list; None when the backend cannot answer."""
        try:
            return int(self._op_get_watchlist({"list_name": list_name})["count"])
        except Exception as e:
            logger.warning("Could not count watchlist '%s': %s", list_name, e)
            return None

    def _drop_open_watchlist(self, list_name: str) -> None:
        """Clear an open watchlist window of a deleted list and forget it."""
        ledger = getattr(self.agent, "layout_ledger", {}) or {}
        info = ledger.get(list_name)
        if not isinstance(info, dict) or info.get("list_id") != list_name:
            return
        try:
            self.agent.update_watchlist_data(list_name, columns=["Symbol"], rows=[], replace=True)
        except Exception as e:
            logger.warning("Could not clear open watchlist '%s': %s", list_name, e)
        ledger.pop(list_name, None)
        autosave = getattr(self.agent, "schedule_autosave", None)
        if callable(autosave):
            try:
                autosave()
            except Exception as e:
                logger.warning("Could not schedule autosave after deleting '%s': %s", list_name, e)

    def _refresh_open_watchlist(self, list_name: str) -> None:
        """Push the refreshed ticker list to an open watchlist window of that list."""
        ledger = getattr(self.agent, "layout_ledger", {}) or {}
        info = ledger.get(list_name)
        if not isinstance(info, dict) or info.get("list_id") != list_name:
            return
        try:
            data = self._op_get_watchlist({"list_name": list_name})
            rows = [{"symbol": t["ticker"], "cells": {"Symbol": t["ticker"]}} for t in data.get("tickers", [])]
            self.agent.update_watchlist_data(list_name, columns=["Symbol"], rows=rows, replace=True)
        except Exception as e:
            logger.warning("Could not refresh open watchlist '%s': %s", list_name, e)

    def _trigger_download(self, tickers: list) -> None:
        """Fire-and-forget: ask stock-data-node to queue the chart download."""
        if not tickers or not self.config.stock_data_url:
            return
        url = f"{self.config.stock_data_url.rstrip('/')}/add"
        payload = {"tickers": list(tickers)}

        def _worker() -> None:
            try:
                self._http_json("POST", url, payload, timeout=self.config.download_timeout_s)
                logger.info("Queued chart download for %s", payload["tickers"])
            except Exception as e:
                logger.warning("Download trigger failed for %s: %s", payload["tickers"], e)

        threading.Thread(target=_worker, name="cv-control-download", daemon=True).start()

    # ── presets ────────────────────────────────────────────────────────────

    def _op_list_presets(self, params: Dict[str, Any]) -> Dict[str, Any]:
        res = self._pca_json("GET", "/api/presets")
        raw = res.get("presets") if isinstance(res, dict) else None
        presets = []
        if isinstance(raw, dict):
            for preset_id, meta in raw.items():
                meta = meta if isinstance(meta, dict) else {}
                presets.append(
                    {
                        "id": str(preset_id),
                        "display_name": str(meta.get("display_name") or preset_id),
                        "description": str(meta.get("description") or ""),
                        "indicator_count": int(meta.get("indicator_count") or 0),
                    }
                )
        elif isinstance(raw, list):  # defensive: list-shaped API response
            for meta in raw:
                if not isinstance(meta, dict):
                    continue
                preset_id = str(meta.get("id") or meta.get("preset_id") or "").strip()
                if preset_id:
                    presets.append(
                        {
                            "id": preset_id,
                            "display_name": str(meta.get("display_name") or preset_id),
                            "description": str(meta.get("description") or ""),
                            "indicator_count": int(meta.get("indicator_count") or 0),
                        }
                    )
        presets.sort(key=lambda p: p["id"])
        return {"presets": presets, "count": len(presets)}

    def _op_apply_preset(self, params: Dict[str, Any]) -> Dict[str, Any]:
        preset_id = str(params.get("preset_id") or "").strip()
        if not preset_id:
            raise ControlError("invalid_request", "preset_id is required")
        try:
            color_flag = int(params.get("color_flag") or 0)
        except (TypeError, ValueError):
            color_flag = 0

        applied = []
        skipped = []
        ledger = getattr(self.agent, "layout_ledger", {}) or {}
        for window_id, info in list(ledger.items()):
            if not isinstance(info, dict) or "list_id" in info:
                continue  # watchlist window or malformed entry
            try:
                info_flag = int(info.get("color_flag") or 0)
            except (TypeError, ValueError):
                info_flag = 0
            if info_flag != color_flag:
                continue
            symbol = str(info.get("symbol") or "").strip()
            if not symbol:
                skipped.append({"window_id": window_id, "reason": "no_symbol"})
                continue
            timeframe = info.get("timeframe") if isinstance(info.get("timeframe"), dict) else {}
            timeframe_str = f"{timeframe.get('multiplier', 1)}{timeframe.get('unit', 'D')}"
            command = {
                "action": "DISPLAY_STOCK",
                "window_id": window_id,
                "symbol": symbol,
                "preset": preset_id,
                "timeframe_str": timeframe_str,
            }
            try:
                self._http_json(
                    "POST",
                    f"{self.config.control_api_url.rstrip('/')}/api/command",
                    command,
                    timeout=self.config.http_timeout_s,
                )
                applied.append(window_id)
            except Exception as e:
                skipped.append({"window_id": window_id, "reason": str(e)})

        return {
            "preset_id": preset_id,
            "color_flag": color_flag,
            "applied": applied,
            "skipped": skipped,
        }

    # ── Pane-/Chart-Ebene (Chart-Builder) ──────────────────────────────────

    def _ledger_chart(self, window_id: str) -> Dict[str, Any]:
        """Ledger-Eintrag eines Chartfensters oder ein sauberer Fehler."""
        ledger = getattr(self.agent, "layout_ledger", {}) or {}
        info = ledger.get(window_id)
        if not isinstance(info, dict) or "list_id" in info:
            raise ControlError("unknown_window", f"Kein Chartfenster '{window_id}' offen.")
        return info

    def _window_timeframe(self, info: Dict[str, Any]) -> str:
        tf = info.get("timeframe")
        if isinstance(tf, dict):
            return f"{tf.get('multiplier', 1)}{tf.get('unit', 'D')}"
        return str(tf or "1D")

    def _clean_pane_specs(self, raw: Any) -> list:
        specs = []
        if not isinstance(raw, list):
            return specs
        for idx, entry in enumerate(raw):
            entry = entry if isinstance(entry, dict) else {}
            preset_id = str(entry.get("pane_preset_id") or entry.get("preset_id") or "").strip()
            if not preset_id:
                continue
            spec = {"pane_preset_id": preset_id}
            pane_id = str(entry.get("pane_id") or "").strip()
            if pane_id:
                spec["pane_id"] = pane_id
            if entry.get("scale"):
                spec["scale"] = str(entry["scale"]).strip().lower()
            try:
                weight = int(entry.get("weight") or 0)
            except (TypeError, ValueError):
                weight = 0
            if weight > 0:
                spec["weight"] = weight
            if isinstance(entry.get("overrides"), dict) and entry["overrides"]:
                spec["overrides"] = entry["overrides"]
            specs.append(spec)
        return specs

    def _render_window(self, window_id: str, extra: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Fenster mit einer Chart-Definition/Pane-Liste neu rendern (DISPLAY_STOCK).

        Ohne Geometrie: ein Re-Render (Compose, Chart anwenden, Speichern,
        Verwerfen) verschiebt kein Fenster - Position und Groesse gehoeren dem
        Client. Ausdrueckliche Platzierungen macht der Agent ueber LOAD_SETUP bzw.
        layout.restore.
        """
        info = self._ledger_chart(window_id)
        command: Dict[str, Any] = {
            "action": "DISPLAY_STOCK",
            "window_id": window_id,
            "symbol": info.get("symbol") or "CHART",
            "timeframe_str": self._window_timeframe(info),
            "limit": 2000,
        }
        command.update({k: v for k, v in extra.items() if v is not None})
        return self._http_json(
            "POST", f"{self.config.control_api_url.rstrip('/')}/api/command", command, timeout=timeout
        )

    def _op_list_panes(self, params: Dict[str, Any]) -> Dict[str, Any]:
        res = self._pca_json("GET", "/api/panes")
        raw = res.get("panes") if isinstance(res, dict) else None
        panes = []
        for pane in raw or []:
            if not isinstance(pane, dict):
                continue
            panes.append(
                {
                    "id": str(pane.get("id") or ""),
                    "display_name": str(pane.get("display_name") or pane.get("id") or ""),
                    "description": str(pane.get("description") or ""),
                    "role": str(pane.get("role") or "any"),
                    "kind": str(pane.get("kind") or "indicator"),
                    "default_scale": str(pane.get("default_scale") or "linear"),
                    "summary": str(pane.get("summary") or ""),
                    "member_count": int(pane.get("member_count") or 0),
                    "zone_count": int(pane.get("zone_count") or 0),
                    "ref_count": int(pane.get("ref_count") or 0),
                }
            )
        # Preispane-Kandidaten zuerst, danach alphabetisch - das Dropdown soll
        # ohne Sortierklick benutzbar sein.
        panes.sort(key=lambda p: (0 if p["role"] in ("price", "any") else 1, p["display_name"].lower()))
        return {"panes": panes, "count": len(panes)}

    def _op_list_charts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        res = self._pca_json("GET", "/api/charts")
        raw = res.get("charts") if isinstance(res, dict) else None
        charts = []
        for chart in raw or []:
            if not isinstance(chart, dict):
                continue
            charts.append(
                {
                    "id": str(chart.get("id") or ""),
                    "display_name": str(chart.get("display_name") or chart.get("id") or ""),
                    "description": str(chart.get("description") or ""),
                    "pane_count": int(chart.get("pane_count") or 0),
                    "summary": str(chart.get("summary") or ""),
                }
            )
        charts.sort(key=lambda c: c["display_name"].lower())
        return {"charts": charts, "count": len(charts)}

    def _op_get_chart_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        window_id = str(params.get("window_id") or "").strip()
        if not window_id:
            raise ControlError("invalid_request", "window_id is required")
        info = self._ledger_chart(window_id)
        chart = info.get("chart") or {}
        panes = []
        for pane in chart.get("panes") or []:
            if not isinstance(pane, dict):
                continue
            panes.append(
                {
                    "pane_id": str(pane.get("pane_id") or ""),
                    "pane_preset_id": str(pane.get("pane_preset_id") or ""),
                    "scale": str(pane.get("scale") or "linear"),
                    "weight": int(pane.get("weight") or 0) or 2,
                    "overrides": pane.get("overrides") or {},
                }
            )
        return {
            "window_id": window_id,
            "symbol": str(info.get("symbol") or ""),
            "timeframe": self._window_timeframe(info),
            "chart_id": chart.get("id"),
            "display_name": str(chart.get("display_name") or ""),
            "draft": bool(chart.get("draft")) or not chart.get("id"),
            "topbar_metrics": chart.get("topbar_metrics") or [],
            "x_axis_pane": chart.get("x_axis_pane"),
            "panes": panes,
        }

    def _op_list_topbar_metrics(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Verfuegbare Topbar-Metriken (Feature-Spalten) auflisten.

        Eine Topbar-Metrik ist eine Feature-Spalte (z.B. ibd_rs, adr_20_pct);
        optional wird geprueft, ob das angegebene Symbol Daten dafuer hat.
        """
        symbol = sanitize_ticker(params.get("symbol"))
        registry = self._pca_json("GET", "/api/features/registry")
        metrics = []
        for feature in registry.get("features") or []:
            if not isinstance(feature, dict):
                continue
            canonical = str(feature.get("canonical_id") or "").strip()
            if not canonical:
                continue
            metrics.append(
                {
                    "id": canonical,
                    "alias": str(feature.get("alias") or ""),
                    "display_name": str(feature.get("display_name") or canonical),
                    "calc_type": feature.get("calc_type"),
                    "has_data_for_symbol": None,
                }
            )
        metrics.sort(key=lambda m: m["display_name"].lower())

        if symbol and metrics:
            columns: set = set()
            try:
                data = self._pca_json(
                    "GET",
                    f"/api/chartdata?symbol={urllib.parse.quote(symbol, safe='')}"
                    "&timeframe=1D&limit=1&features=true",
                )
                columns = {str(c) for c in (data.get("columns") or [])}
            except ControlError:
                columns = set()
            for metric in metrics:
                candidates = {metric["id"], metric["alias"]} - {""}
                metric["has_data_for_symbol"] = bool(candidates & columns)

        return {"symbol": symbol or None, "metrics": metrics, "count": len(metrics)}

    def _op_list_windows(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Alle offenen Fenster mit Inhalt (Chart/Symbol/Timeframe) auflisten."""
        ledger = getattr(self.agent, "layout_ledger", {}) or {}
        windows = []
        for window_id, info in ledger.items():
            if not isinstance(info, dict):
                continue
            if "list_id" in info:  # Watchlist-Fenster (kein Chartfenster)
                windows.append(
                    {
                        "window_id": str(window_id),
                        "kind": "watchlist",
                        "list_id": str(info.get("list_id") or ""),
                        "display_name": str(info.get("display_name") or info.get("list_id") or ""),
                        "symbol": "",
                        "timeframe": "",
                        "chart_id": None,
                        "draft": False,
                        "pane_count": 0,
                        "topbar_metric_count": 0,
                    }
                )
                continue
            chart = info.get("chart") or {}
            panes = [p for p in (chart.get("panes") or []) if isinstance(p, dict)]
            windows.append(
                {
                    "window_id": str(window_id),
                    "kind": "chart",
                    "symbol": str(info.get("symbol") or ""),
                    "timeframe": self._window_timeframe(info),
                    "chart_id": chart.get("id"),
                    "display_name": str(chart.get("display_name") or ""),
                    "draft": bool(chart.get("draft")) or not chart.get("id"),
                    "pane_count": len(panes),
                    "topbar_metric_count": len(chart.get("topbar_metrics") or []),
                }
            )
        # Chartfenster zuerst, danach stabil nach window_id.
        windows.sort(key=lambda w: (w["kind"] != "chart", w["window_id"]))
        return {"windows": windows, "count": len(windows)}

    def _op_compose_chart(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Ad-hoc-Zusammenstellung rendern (Builder-Draft, noch nicht gespeichert)."""
        window_id = str(params.get("window_id") or "").strip()
        if not window_id:
            raise ControlError("invalid_request", "window_id is required")
        info = self._ledger_chart(window_id)
        specs = self._clean_pane_specs(params.get("panes"))
        if not specs:
            raise ControlError("invalid_request", "panes darf nicht leer sein")

        base_id = str(params.get("base_chart_id") or "").strip()
        base: Dict[str, Any] = {}
        if base_id:
            try:
                base = self._pca_json("GET", f'/api/charts/{urllib.parse.quote(base_id, safe="")}')
            except ControlError:
                base = {}
        else:
            # Kein Basis-Chart angegeben: Chart-Metadaten des Fensters erben, damit
            # ein Pane-Edit den Topbar-Streifen nicht stillschweigend leert.
            base = info.get("chart") or {}

        topbar = params.get("topbar_metrics")
        if topbar is None:
            topbar = base.get("topbar_metrics") or []
        x_axis = params.get("x_axis_pane")
        if x_axis is None:
            x_axis = base.get("x_axis_pane")

        render = self._render_window(
            window_id,
            {
                "panes": specs,
                "chart_meta": {
                    "id": base_id or None,
                    "display_name": str(params.get("display_name") or (base.get("display_name") if base_id else "") or "• unbenannt"),
                    "draft": True,
                    "topbar_metrics": list(topbar or []),
                    "x_axis_pane": x_axis,
                },
            },
            timeout=45.0,
        )
        return {"window_id": window_id, "draft": True, "panes": specs, "render": render}

    def _op_apply_chart(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Gespeichertes Chart auf ein Fenster anwenden."""
        window_id = str(params.get("window_id") or "").strip()
        chart_id = str(params.get("chart_id") or "").strip()
        if not window_id or not chart_id:
            raise ControlError("invalid_request", "window_id und chart_id sind Pflicht")
        self._ledger_chart(window_id)
        render = self._render_window(window_id, {"chart": chart_id}, timeout=45.0)
        return {"window_id": window_id, "chart_id": chart_id, "render": render}

    def _op_diff_chart(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fensterinhalt gegen eine gespeicherte Chart-Definition vergleichen.

        Rendert nichts: der Vergleich ist ein Mengen-/Reihenfolge-/Feldervergleich
        der Pane-Slots (chart_spec.diff_chart_specs).
        """
        window_id = str(params.get("window_id") or "").strip()
        chart_id = str(params.get("chart_id") or "").strip()
        if not window_id or not chart_id:
            raise ControlError("invalid_request", "window_id und chart_id sind Pflicht")
        info = self._ledger_chart(window_id)
        current = info.get("chart") or {}
        target = self._pca_json("GET", f'/api/charts/{urllib.parse.quote(chart_id, safe="")}')
        diff = chart_spec.diff_chart_specs(
            current.get("panes"),
            target.get("panes"),
            {"topbar_metrics": current.get("topbar_metrics"), "x_axis_pane": current.get("x_axis_pane")},
            {"topbar_metrics": target.get("topbar_metrics"), "x_axis_pane": target.get("x_axis_pane")},
        )
        return {
            "window_id": window_id,
            "chart_id": chart_id,
            "window_chart_id": current.get("id"),
            "draft": bool(current.get("draft")) or not current.get("id"),
            **diff,
        }

    def _op_save_chart(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Aktuelle Zusammensetzung des Fensters als Chart speichern."""
        window_id = str(params.get("window_id") or "").strip()
        chart_id = str(params.get("chart_id") or "").strip()
        if not window_id or not chart_id:
            raise ControlError("invalid_request", "window_id und chart_id sind Pflicht")
        info = self._ledger_chart(window_id)
        chart = info.get("chart") or {}
        panes = self._clean_pane_specs(chart.get("panes"))
        if not panes:
            raise ControlError("invalid_request", "Das Fenster hat keine Panes zum Speichern")

        payload = {
            "id": chart_id,
            "display_name": str(params.get("display_name") or chart_id),
            "description": str(params.get("description") or ""),
            "topbar_metrics": chart.get("topbar_metrics") or [],
            "x_axis_pane": chart.get("x_axis_pane"),
            "panes": panes,
        }
        # overwrite=false schuetzt vor stillem Ueberschreiben (Default: heutiges Verhalten).
        overwrite = params.get("overwrite")
        overwrite = True if overwrite is None else bool(overwrite)

        exists = True
        try:
            self._pca_json("GET", f'/api/charts/{urllib.parse.quote(chart_id, safe="")}')
        except ControlError:
            exists = False
        if exists and not overwrite:
            raise ControlError(
                "already_exists",
                f"Chart '{chart_id}' existiert bereits (overwrite=false).",
            )
        if exists:
            result = self._pca_json("PUT", f'/api/charts/{urllib.parse.quote(chart_id, safe="")}', payload)
        else:
            result = self._pca_json("POST", "/api/charts", payload)

        # Fenster auf den gespeicherten Stand ziehen (Draft-Markierung faellt weg).
        try:
            self._render_window(window_id, {"chart": chart_id}, timeout=45.0)
        except Exception as exc:  # noqa: BLE001 - Speichern ist schon durch
            logger.warning("Chart '%s' gespeichert, Re-Render fehlgeschlagen: %s", chart_id, exc)

        return {
            "window_id": window_id,
            "chart_id": chart_id,
            "saved": True,
            "created": not exists,
            "backend": result,
        }

    # ── pane scales (viewer LOG/LIN buttons) ───────────────────────────────

    def set_pane_scales(self, preset_id: str, pane_scales: Any) -> Dict[str, Any]:
        """Persist the Y-scale of individual panes in a preset.

        Called by the agent when the viewer reports a LOG/LIN toggle. Uses the
        dedicated PATCH endpoint (merge), so preset members and styles are never
        rewritten by a UI click. Returns the merged mapping from the PCA service.
        """
        clean_id = str(preset_id or "").strip()
        if not clean_id:
            raise ControlError("invalid_request", "preset_id is required")
        payload = {"pane_scales": sanitize_pane_scales(pane_scales)}
        return self._pca_json(
            "PATCH",
            f'/api/presets/{urllib.parse.quote(clean_id, safe="")}/pane_scales',
            payload,
        )

    # ── HTTP helpers ───────────────────────────────────────────────────────

    def _pca_json(self, method: str, path: str, payload: Optional[dict] = None) -> Dict[str, Any]:
        url = f"{self.config.pca_url.rstrip('/')}{path}"
        return self._http_json(method, url, payload)

    def _http_json(
        self,
        method: str,
        url: str,
        payload: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.config.http_timeout_s) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")
            except Exception:
                pass
            message = f"HTTP {e.code}: {detail or e.reason}"
            if e.code == 404:
                raise ControlError("not_found", message) from e
            if e.code == 400:
                lowered = detail.lower()
                if "master" in lowered or "geschützt" in lowered or "geschuetzt" in lowered:
                    raise ControlError("protected_list", message) from e
                raise ControlError("invalid_request", message) from e
            raise SupabaseError(message, status=e.code) from e
        except urllib.error.URLError as e:
            raise SupabaseError(f"Unreachable: {extract_url_error_msg(e)}") from e

        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {"raw": body}
        return parsed if isinstance(parsed, dict) else {"data": parsed}
