"""Reference Python Agent implementation according to Section 1, 8, & 11."""

from __future__ import annotations
import json
import logging
import os
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional
from chart_viewer.transport.base import AgentTransport
from chart_viewer.models.envelope import Envelope, MessageKind, make_envelope, create_message_id
from chart_viewer.models.entities import WindowState, WindowGeometry, MonitorInfo

logger = logging.getLogger(__name__)

# Configurable timeouts and debounce intervals (via environment variables)
CV_SUPABASE_TIMEOUT_SEC = float(os.environ.get("CV_SUPABASE_TIMEOUT_SEC", "10.0"))
CV_AUTOSAVE_DEBOUNCE_SEC = float(os.environ.get("CV_AUTOSAVE_DEBOUNCE_SEC", "1.5"))

# Supabase configuration (same as agent-pca shared.ts)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://host.docker.internal:8001")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_REST_URL = f"{SUPABASE_URL}/rest/v1"
SUPABASE_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def _extract_url_error_msg(e: urllib.error.URLError) -> str:
    """Extract informative error text from URLError / HTTPError."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode(errors="replace")
            return f"HTTP {e.code}: {e.reason} - {body}"
        except Exception:
            return f"HTTP {e.code}: {e.reason}"
    return str(e)


def _supabase_get(path: str) -> dict | list:
    """GET from Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    req = urllib.request.Request(url, headers=SUPABASE_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=CV_SUPABASE_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        msg = _extract_url_error_msg(e)
        logger.error("Supabase GET %s failed: %s", url, msg)
        raise RuntimeError(f"Supabase unreachable: {msg}") from e


def _supabase_post(path: str, payload: dict) -> dict | list:
    """POST to Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=SUPABASE_HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=CV_SUPABASE_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        msg = _extract_url_error_msg(e)
        logger.error("Supabase POST %s failed: %s", url, msg)
        raise RuntimeError(f"Supabase unreachable: {msg}") from e


def _supabase_patch(path: str, payload: dict) -> dict | list:
    """PATCH to Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=SUPABASE_HEADERS, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=CV_SUPABASE_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        msg = _extract_url_error_msg(e)
        logger.error("Supabase PATCH %s failed: %s", url, msg)
        raise RuntimeError(f"Supabase unreachable: {msg}") from e


def _supabase_delete(path: str) -> None:
    """DELETE from Supabase REST API."""
    url = f"{SUPABASE_REST_URL}{path}"
    req = urllib.request.Request(url, headers=SUPABASE_HEADERS, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=CV_SUPABASE_TIMEOUT_SEC):
            pass
    except urllib.error.URLError as e:
        msg = _extract_url_error_msg(e)
        logger.error("Supabase DELETE %s failed: %s", url, msg)
        raise RuntimeError(f"Supabase unreachable: {msg}") from e


class ChartAgent:
    """Reference Python Agent: sole authoritative source of truth for layout and data."""

    def __init__(self, transport: AgentTransport):
        self.transport = transport
        self.transport.on_event(self._on_envelope)

        # Authoritative Layout Ledger (Section 11)
        self.layout_ledger: Dict[str, dict] = {}
        self.series_data: Dict[str, dict] = {}
        self.received_events: List[Envelope] = []
        self._pending_screenshots: Dict[str, dict] = {}
        self._seq = 0
        self.on_viewer_ready_callback = None

        # Window Setup Management
        self.current_setup_name: str | None = None  # None → Autosave "default" active
        self._last_autosave_hash: str = ""  # Dedup hash to avoid redundant saves
        self.screens: list[dict] = []
        self._autosave_timer: threading.Timer | None = None
        self._autosave_lock = threading.Lock()

    def start(self) -> None:
        self.transport.connect()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def schedule_autosave(self) -> None:
        """Schedule an asynchronous debounced autosave (non-blocking)."""
        if self.current_setup_name is not None and self.current_setup_name != "default":
            return
        with self._autosave_lock:
            if self._autosave_timer is not None:
                self._autosave_timer.cancel()
            self._autosave_timer = threading.Timer(CV_AUTOSAVE_DEBOUNCE_SEC, self._debounced_autosave_worker)
            self._autosave_timer.daemon = True
            self._autosave_timer.start()

    def _debounced_autosave_worker(self) -> None:
        """Worker thread executing the debounced autosave."""
        try:
            self.autosave_default()
        except Exception as e:
            logger.debug("Debounced autosave execution skipped: %s", e)

    def send_command(self, env: Envelope) -> None:
        """Send command with backoff retry if transport is not connected.
        
        Design decision: This retries on transport-layer failures only (disconnect,
        send exceptions). It does NOT wait for ACKs from the viewer. Rationale:
        - The viewer always sends ACKs for commands (window.open, snapshot.full, etc.)
        - Sequence-gap detection in the transport layer triggers resync if messages are lost
        - Full ACK-tracking with per-message threading.Events would add complexity
          disproportionate to the risk (WebSocket over localhost/TCP is reliable)
        - screenshot.request is the only command that needs response correlation,
          and it already has its own threading.Event mechanism
        
        If silent packet loss becomes a problem, implement ACK-based retry as specified
        in IMPLEMENTATIONSPLAN.md Maßnahme 7.
        """
        import time
        delays = [0.1, 0.5, 1.0]
        
        for attempt, delay in enumerate(delays, start=1):
            connected = getattr(self.transport, "_connected", True)
            if not connected:
                logger.warning(f"Transport not connected, retrying in {delay}s (attempt {attempt})")
                time.sleep(delay)
                continue
            try:
                self.transport.send_command(env)
                return
            except Exception as e:
                logger.warning(f"Transport send failed, retrying in {delay}s (attempt {attempt}): {e}")
                time.sleep(delay)
        
        # Final attempt
        self.transport.send_command(env)

    def _on_envelope(self, envelope: Envelope) -> None:
        self.received_events.append(envelope)
        logger.info(f"Agent received: {envelope.type} (kind={envelope.kind})")

        if envelope.type == "viewer.ready":
            self._handle_viewer_ready(envelope)
            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            if "screens" in payload and isinstance(payload["screens"], list):
                self.screens = payload["screens"]
                logger.info("Agent recorded %d connected screens from viewer.ready", len(self.screens))

        elif envelope.type == "window.closed":
            win_id = envelope.window_id or (envelope.payload.get("window_id") if isinstance(envelope.payload, dict) else None)
            if win_id:
                logger.info(f"Agent recorded window {win_id} closed by user")
                self.layout_ledger.pop(win_id, None)
                self.series_data.pop(win_id, None)

        elif envelope.type == "window.geometry_changed":
            win_id = envelope.window_id or (envelope.payload.get("window_id") if isinstance(envelope.payload, dict) else None)
            geom = envelope.payload.get("geometry") if isinstance(envelope.payload, dict) else {}
            if win_id and geom and win_id in self.layout_ledger:
                self.layout_ledger[win_id]["position"] = {
                    "x": geom.get("x", 0),
                    "y": geom.get("y", 0),
                }
                self.layout_ledger[win_id]["size"] = {
                    "width": geom.get("width", 0),
                    "height": geom.get("height", 0),
                }
            # Trigger debounced autosave on geometry changes
            self.schedule_autosave()

        elif envelope.type == "window.change_symbol":
            win_id = envelope.window_id or (envelope.payload.get("window_id") if isinstance(envelope.payload, dict) else None)
            symbol = envelope.payload.get("symbol") if isinstance(envelope.payload, dict) else None
            if win_id and symbol:
                self._handle_window_change_symbol(win_id, symbol)

        elif envelope.type == "annotation.moved":
            logger.info(f"Agent recorded annotation move: {envelope.payload}")

        elif envelope.type == "resync.request":
            # Viewer requested full resync -> push layout.restore
            self.restore_layout()

        elif envelope.type == "screenshot.response":
            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            req_id = payload.get("request_id")
            if req_id and req_id in self._pending_screenshots:
                entry = self._pending_screenshots[req_id]
                entry["result"] = payload
                entry["event"].set()

        elif envelope.type == "monitor.change":
            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            if "screens" in payload and isinstance(payload["screens"], list):
                self.screens = payload["screens"]
            logger.info("Monitor %s: %d total screens now", payload.get("change_type"), len(self.screens) or payload.get("total_screens", 0))
            # Trigger debounced autosave to update "default" setup with new monitor count
            self.schedule_autosave()


    def _handle_viewer_ready(self, envelope: Envelope) -> None:
        """Viewer connected -> push stored layout if any (Section 11)."""
        logger.info("Viewer ready, restoring layout...")
        self.restore_layout()
        if getattr(self, "on_viewer_ready_callback", None):
            self.on_viewer_ready_callback()

    def open_window(
        self,
        window_id: str,
        symbol: str,
        timeframe_unit: str = "D",
        multiplier: int = 1,
        sync_group_id: Optional[str] = None,
        position: Optional[dict] = None,
        size: Optional[dict] = None,
    ) -> None:
        """Agent opens a new window in the Viewer (Section 8)."""
        win_info = {
            "window_id": window_id,
            "symbol": symbol,
            "timeframe": {"unit": timeframe_unit, "multiplier": multiplier},
            "sync_group_id": sync_group_id,
            "position": position or {"x": 100, "y": 100},
            "size": size or {"width": 900, "height": 600},
        }
        self.layout_ledger[window_id] = win_info

        env = make_envelope(
            msg_type="window.open",
            payload=win_info,
            kind=MessageKind.COMMAND,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.send_command(env)

    def send_snapshot(self, window_id: str, snapshot_data: dict) -> None:
        """Push initial/updated full snapshot to window."""
        self.series_data[window_id] = snapshot_data
        env = make_envelope(
            msg_type="snapshot.full",
            payload=snapshot_data,
            kind=MessageKind.COMMAND,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.send_command(env)

    def open_watchlist(
        self,
        list_id: str,
        display_name: str,
        columns: list[str],
        rows: list[dict],
        color_flag: int = 0,
        sort_column: str | None = None,
        sort_ascending: bool = True,
        position: dict | None = None,
        size: dict | None = None,
    ) -> None:
        """Push a watchlist to the viewer."""
        wl_info = {
            "window_id": list_id,
            "list_id": list_id,
            "display_name": display_name,
            "columns": columns,
            "rows": rows,
            "color_flag": color_flag,
            "sort_column": sort_column,
            "sort_ascending": sort_ascending,
        }
        if position:
            wl_info["position"] = position
        if size:
            wl_info["size"] = size

        self.layout_ledger[list_id] = wl_info

        env = make_envelope(
            msg_type="watchlist.open",
            payload=wl_info,
            kind=MessageKind.COMMAND,
            window_id=list_id,
            sequence=self._next_seq(),
        )
        self.send_command(env)

    def update_watchlist_data(self, list_id: str, columns: list[str] | None = None, rows: list[dict] | None = None, replace: bool = False) -> None:
        """Update rows/columns of an existing watchlist."""
        payload = {"replace": replace}
        if columns is not None:
            payload["columns"] = columns
        if rows is not None:
            payload["rows"] = rows

        env = make_envelope(
            msg_type="watchlist.data",
            payload=payload,
            kind=MessageKind.COMMAND,
            window_id=list_id,
            sequence=self._next_seq(),
        )
        self.send_command(env)

    def _handle_window_change_symbol(self, window_id: str, new_symbol: str) -> None:
        """Fetch new data and send snapshot.full when Chart changes symbol."""
        logger.info(f"Agent processing symbol change for {window_id} -> {new_symbol}")
        
        # We need to preserve the timeframe and preset (if any) from the layout ledger
        win_info = self.layout_ledger.get(window_id, {})
        tf = win_info.get("timeframe", {})
        if isinstance(tf, dict):
            tf_unit = tf.get("unit", "D")
            tf_mult = tf.get("multiplier", 1)
        else:
            tf_unit = win_info.get("timeframe_unit", "D")
            tf_mult = win_info.get("multiplier", 1)
        tf_str = f"{tf_mult}{tf_unit}"
        
        preset = win_info.get("preset")
        if not preset:
            try:
                import json
                with open("/tmp/last_chart_state.json", "r") as f:
                    last_state = json.load(f)
                    preset = last_state.get("preset", "default")
            except Exception:
                preset = "default"

        # Reconstruct the DISPLAY_STOCK command and send it to our own POST API
        # This is the cleanest way because run_server.py does all the fetching + sending
        try:
            import urllib.request
            import json
            
            payload = {
                "action": "DISPLAY_STOCK",
                "window_id": window_id,
                "symbol": new_symbol,
                "timeframe_str": tf_str,
                "preset": preset,
            }
            if "position" in win_info:
                payload["position"] = win_info["position"]
            if "size" in win_info:
                payload["size"] = win_info["size"]
            
            req = urllib.request.Request(
                "http://127.0.0.1:8766/api/command",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req)
        except Exception as e:
            logger.error(f"Failed to fetch new symbol data: {e}")

    def append_bar(self, window_id: str, bar_data: dict) -> None:
        """Push completed candle."""
        env = make_envelope(
            msg_type="bar.append",
            payload={"bar": bar_data},
            kind=MessageKind.COMMAND,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.send_command(env)

    def send_tick(self, window_id: str, price: float, volume: float = 0.0) -> None:
        """Push real-time price tick (fire-and-forget, coalesced)."""
        env = make_envelope(
            msg_type="tick.update",
            payload={"price": price, "volume": volume},
            kind=MessageKind.EVENT,
            window_id=window_id,
            sequence=self._next_seq(),
        )
        self.send_command(env)

    def restore_layout(self) -> None:
        """Section 11: Push layout.restore containing all open windows."""
        windows_list = list(self.layout_ledger.values())
        if not windows_list:
            return

        env = make_envelope(
            msg_type="layout.restore",
            payload={"windows": windows_list},
            kind=MessageKind.COMMAND,
            sequence=self._next_seq(),
        )
        self.send_command(env)

        # Push snapshots for each window
        for win_id, snap in self.series_data.items():
            self.send_snapshot(win_id, snap)

    def request_screenshots(
        self,
        window_id: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        mode: Optional[str] = None,
        timeout_s: float = 10.0,
        sharpen_amount: Optional[float] = None,
        hires: Optional[bool] = None,
    ) -> dict:
        """Send screenshot.request command to viewer clients and wait for response."""
        import threading
        import uuid
        from datetime import datetime

        req_id = f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        event = threading.Event()
        self._pending_screenshots[req_id] = {"event": event, "result": None}

        payload: dict = {
            "request_id": req_id,
            "window_id": window_id,
        }
        if width:
            payload["width"] = width
        if height:
            payload["height"] = height
        if mode:
            payload["mode"] = mode
        if sharpen_amount is not None:
            payload["sharpen_amount"] = sharpen_amount
        if hires is not None:
            payload["hires"] = hires


        env = make_envelope(
            msg_type="screenshot.request",
            payload=payload,
            kind=MessageKind.COMMAND,
            sequence=self._next_seq(),
        )
        self.send_command(env)

        completed = event.wait(timeout=timeout_s)
        entry = self._pending_screenshots.pop(req_id, None)

        if not completed or not entry or entry["result"] is None:
            raise TimeoutError(f"Screenshot request timed out after {timeout_s}s")

        return entry["result"]

    # ── Window Setup Management ────────────────────────────────────────

    def _build_geometry_snapshot(
        self,
        geometries: dict | None = None,
        monitor_infos: list[dict] | None = None,
    ) -> dict:
        """Build a hashable snapshot of current layout state for autosave dedup."""
        import hashlib

        parts = []
        for win_id, win_info in sorted(self.layout_ledger.items()):
            geom = (geometries or {}).get(win_id, win_info.get("position", {}))
            parts.append(json.dumps({
                "id": win_id,
                "type": "watchlist" if "list_id" in win_info or win_id.startswith("wl_") else "chart",
                "x": geom.get("x", 0),
                "y": geom.get("y", 0),
                "w": geom.get("width", win_info.get("size", {}).get("width", 0)),
                "h": geom.get("height", win_info.get("size", {}).get("height", 0)),
                "flag": win_info.get("color_flag", 0),
            }, sort_keys=True))

        if monitor_infos:
            parts.append(json.dumps(sorted(monitor_infos, key=lambda m: m.get("index", 0)), sort_keys=True))

        raw = "|".join(parts)
        return hashlib.md5(raw.encode()).hexdigest()

    def save_setup(
        self,
        setup_name: str,
        geometries: dict | None = None,
        monitor_infos: list[dict] | None = None,
        force: bool = False,
    ) -> dict:
        """Save current window layout as a named setup to Supabase.

        Args:
            setup_name: Name of the setup to save.
            geometries: Optional {window_id: {x, y, width, height}} from viewer.
            monitor_infos: Optional list of monitor dicts.
            force: If True, save even if unchanged.

        Returns:
            {"status": "ok", "setup_name": ..., "monitor_count": ..., "window_count": ...}
        """
        if not setup_name:
            raise ValueError("setup_name is required")

        # Stop autosave for non-default setups
        if setup_name != "default":
            self.current_setup_name = setup_name

        effective_monitors = monitor_infos if monitor_infos is not None else self.screens
        monitor_count = len(effective_monitors) if effective_monitors else 1

        # Build window descriptors from layout_ledger + live geometries
        windows_list = []
        for win_id, win_info in self.layout_ledger.items():
            geom = (geometries or {}).get(win_id, {})
            win_type = "watchlist" if ("list_id" in win_info or win_id.startswith("wl_")) else "chart"

            monitor = None
            if effective_monitors and geom:
                cx = geom.get("x", 0) + geom.get("width", 0) // 2
                cy = geom.get("y", 0) + geom.get("height", 0) // 2
                for mi in effective_monitors:
                    mx, my, mw, mh = mi.get("x", 0), mi.get("y", 0), mi.get("width", 0), mi.get("height", 0)
                    if mx <= cx < mx + mw and my <= cy < my + mh:
                        monitor = mi
                        break

            windows_list.append({
                "window_id": win_id,
                "window_type": win_type,
                "position": {
                    "x": geom.get("x", win_info.get("position", {}).get("x", 100)),
                    "y": geom.get("y", win_info.get("position", {}).get("y", 100)),
                },
                "size": {
                    "width": geom.get("width", win_info.get("size", {}).get("width", 900)),
                    "height": geom.get("height", win_info.get("size", {}).get("height", 600)),
                },
                "color_flag": win_info.get("color_flag", 0),
                "monitor": monitor,
            })

        current_hash = self._build_geometry_snapshot(geometries, effective_monitors)

        if not force:
            if current_hash == self._last_autosave_hash and setup_name == "default":
                logger.debug("Autosave skipped (unchanged geometry)")
                return {"status": "ok", "setup_name": setup_name, "monitor_count": monitor_count,
                        "window_count": len(windows_list), "skipped": True}

        # UPSERT via POST (Supabase REST: use on_conflict on unique columns + resolution=merge-duplicates)
        url = f"{SUPABASE_REST_URL}/pca_window_setups?on_conflict=setup_name,monitor_count"
        data = json.dumps({
            "setup_name": setup_name,
            "monitor_count": monitor_count,
            "windows": windows_list,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).encode()
        headers = {
            **SUPABASE_HEADERS,
            "Prefer": "resolution=merge-duplicates,return=representation",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=CV_SUPABASE_TIMEOUT_SEC) as resp:
                result = json.loads(resp.read().decode())
            logger.info("Setup '%s' saved (%d windows, %d monitors)", setup_name, len(windows_list), monitor_count)

            if setup_name == "default":
                self._last_autosave_hash = current_hash if not force else ""

            return {"status": "ok", "setup_name": setup_name, "monitor_count": monitor_count,
                    "window_count": len(windows_list)}
        except urllib.error.URLError as e:
            msg = _extract_url_error_msg(e)
            logger.error("Failed to save setup '%s': %s", setup_name, msg)
            raise RuntimeError(f"Failed to save setup: {msg}") from e

    def autosave_default(self, geometries: dict | None = None, monitor_infos: list[dict] | None = None) -> None:
        """Trigger autosave to 'default' setup — only when current_setup_name is None or 'default'."""
        if self.current_setup_name is not None and self.current_setup_name != "default":
            return  # Autosave disabled for loaded named setups
        monitors = monitor_infos if monitor_infos is not None else self.screens
        try:
            self.save_setup("default", geometries=geometries, monitor_infos=monitors)
        except Exception as e:
            logger.warning("Autosave failed: %s", e)

    def load_setup(
        self,
        setup_name: str,
        current_monitor_count: int | None = None,
    ) -> dict:
        """Load a window setup from Supabase.

        Matching strategy:
        1. Exact monitor_count match
        2. Closest monitor_count (if no exact match)

        Returns a command dict with windows-to-open, reuse-mappings, and windows-to-close.
        """
        if not setup_name:
            raise ValueError("setup_name is required")

        target_monitors = current_monitor_count if current_monitor_count is not None else (len(self.screens) or 1)

        # Query all variants for this setup_name
        try:
            encoded_name = urllib.parse.quote(setup_name)
            rows = _supabase_get(
                f"/pca_window_setups?setup_name=eq.{encoded_name}&order=monitor_count.asc"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to query setups: {e}") from e

        if not rows:
            raise RuntimeError(f"Setup '{setup_name}' not found")

        # Find exact or closest match
        if isinstance(rows, list):
            best = None
            best_diff = 999
            for row in rows:
                diff = abs(row.get("monitor_count", 1) - target_monitors)
                if diff < best_diff:
                    best_diff = diff
                    best = row
            row = best
        else:
            row = rows

        if not row:
            raise RuntimeError(f"Setup '{setup_name}' has no matching variant")

        windows = row.get("windows", [])
        src_monitor_count = row.get("monitor_count", 1)

        # Build list of windows to open
        windows_to_open = []
        for i, w in enumerate(windows):
            pos = w.get("position", {"x": 100, "y": 100})
            size = w.get("size", {"width": 900, "height": 600})

            # Remap position if monitor count differs
            if src_monitor_count != target_monitors and target_monitors > 0:
                # If more source monitors than current, redistribute
                src_mon = w.get("monitor", {})
                src_idx = src_mon.get("index", 0) if src_mon else (i % src_monitor_count)

                if src_idx >= target_monitors:
                    # Window was on a monitor that doesn't exist → distribute
                    target_idx = i % target_monitors
                    # Apply cascade offset for multiple windows on same monitor
                    cascade_x = (i // target_monitors) * 40
                    cascade_y = (i // target_monitors) * 40
                    if isinstance(pos, dict):
                        pos = {"x": pos.get("x", 100) + cascade_x,
                               "y": pos.get("y", 100) + cascade_y}

            windows_to_open.append({
                "window_id": w.get("window_id", f"win_setup_{i}"),
                "window_type": w.get("window_type", "chart"),
                "position": pos,
                "size": size,
                "color_flag": w.get("color_flag", 0),
            })

        logger.info(
            "Setup '%s' loaded: %d windows (source_monitors=%d, current=%d, diff=%d)",
            setup_name, len(windows), src_monitor_count, target_monitors,
            abs(src_monitor_count - target_monitors),
        )

        return {
            "status": "ok",
            "action": "LOAD_SETUP",
            "setup_name": row.get("setup_name", setup_name),
            "monitor_count": target_monitors,
            "source_monitor_count": src_monitor_count,
            "windows": windows_to_open,
            "close_existing": True,  # Close windows not in setup
        }

    def list_setups(self) -> list[dict]:
        """List all saved setups grouped by name with monitor variants."""
        try:
            rows = _supabase_get("/pca_window_setups?order=setup_name.asc,monitor_count.asc")
        except Exception as e:
            raise RuntimeError(f"Failed to list setups: {e}") from e

        if not isinstance(rows, list):
            rows = []

        # Group by setup_name
        groups: dict[str, list[dict]] = {}
        for row in rows:
            name = row.get("setup_name", "unknown")
            groups.setdefault(name, []).append({
                "id": row.get("id", ""),
                "monitor_count": row.get("monitor_count", 1),
                "window_count": len(row.get("windows", [])),
                "updated_at": row.get("updated_at", ""),
            })

        return [{"setup_name": name, "variants": variants} for name, variants in sorted(groups.items())]

    def delete_setup(self, setup_name: str, monitor_count: int | None = None) -> dict:
        """Delete a setup variant or all variants of a setup.

        Args:
            setup_name: Name of the setup to delete.
            monitor_count: Optional specific monitor count to delete. If None, deletes all variants.
        """
        try:
            encoded_name = urllib.parse.quote(setup_name)
            if monitor_count is not None:
                path = f"/pca_window_setups?setup_name=eq.{encoded_name}&monitor_count=eq.{monitor_count}"
            else:
                path = f"/pca_window_setups?setup_name=eq.{encoded_name}"
            _supabase_delete(path)
            logger.info("Deleted setup '%s' (monitor_count=%s)", setup_name, monitor_count or "ALL")
            return {"status": "ok", "setup_name": setup_name, "monitor_count": monitor_count}
        except Exception as e:
            raise RuntimeError(f"Failed to delete setup: {e}") from e

    def rename_setup(self, setup_name: str, new_setup_name: str) -> dict:
        """Rename all variants of a setup."""
        if not new_setup_name:
            raise ValueError("new_setup_name is required")

        try:
            encoded_name = urllib.parse.quote(setup_name)
            _supabase_patch(
                f"/pca_window_setups?setup_name=eq.{encoded_name}",
                {"setup_name": new_setup_name, "updated_at": datetime.now(timezone.utc).isoformat()},
            )
            logger.info("Renamed setup '%s' → '%s'", setup_name, new_setup_name)

            # If the renamed setup was current, update tracker
            if self.current_setup_name == setup_name:
                self.current_setup_name = new_setup_name

            return {"status": "ok", "old_name": setup_name, "new_name": new_setup_name}
        except Exception as e:
            raise RuntimeError(f"Failed to rename setup: {e}") from e

