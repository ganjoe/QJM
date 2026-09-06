"""ViewerApp managing overall application lifecycle and transport dispatch according to Section 8."""

from __future__ import annotations
import logging
from typing import Dict, Optional
from PySide6.QtCore import QObject, QTimer, Signal, QPointF
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication

from chart_viewer.config import ViewerConfig
from chart_viewer.transport.base import AgentTransport
from chart_viewer.models.envelope import Envelope, MessageKind, make_envelope
from chart_viewer.core.event_hub import EventHub
from chart_viewer.core.state_manager import StateManager
from chart_viewer.core.backpressure import TickCoalescer
from chart_viewer.ui.window import ChartWindow
from chart_viewer.ui.watchlist_window import WatchlistWindow
from chart_viewer.models.entities import WindowState, Timeframe

logger = logging.getLogger(__name__)


class ViewerApp(QObject):
    """Main application controller binding AgentTransport, EventHub, StateManager, and Windows."""

    # Thread-safe signal to process envelopes on the Qt GUI thread
    envelope_received_signal = Signal(object)

    def __init__(
        self,
        transport: AgentTransport,
        config: ViewerConfig,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.transport = transport
        self.config = config

        self.event_hub = EventHub()
        self.state_manager = StateManager()
        self.windows: Dict[str, ChartWindow] = {}
        self.watchlists: Dict[str, WatchlistWindow] = {}

        # Connect Qt thread-safe signal for incoming transport events
        self.envelope_received_signal.connect(self._handle_envelope_gui_thread)
        self.transport.on_event(self._on_transport_event)

        # Connect EventHub crosshair sync
        self.event_hub.on_crosshair_broadcast(self._on_crosshair_broadcast_received)

        # Backpressure Coalescer + Render Timer (Section 3.4)
        self.coalescer = TickCoalescer(on_flush=self._on_tick_flushed, config=self.config)
        self.render_timer = QTimer(self)
        self.render_timer.setInterval(self.config.max_fps_interval_ms)  # ~16ms for 60Hz
        self.render_timer.timeout.connect(self.coalescer.flush)

        # Monitor connect/disconnect detection (for multi-monitor setup management)
        gui_app = QGuiApplication.instance()
        if gui_app is not None:
            gui_app.screenAdded.connect(self._on_screen_added)
            gui_app.screenRemoved.connect(self._on_screen_removed)

    def start(self) -> None:
        """Start application: connect transport and start 60Hz render timer.

        Section 8: Starts with 0 windows. Viewer creates windows only on agent command.
        """
        self.render_timer.start()
        self.transport.connect()

    def _send_viewer_ready(self) -> None:
        """Handshake: send viewer.ready with screen info (Section 8)."""
        screen = QGuiApplication.primaryScreen()
        screen_info = {}
        if screen:
            geom = screen.geometry()
            screen_info = {
                "width": geom.width(),
                "height": geom.height(),
                "device_pixel_ratio": screen.devicePixelRatio(),
            }

        ready_env = make_envelope(
            msg_type="viewer.ready",
            payload={
                "protocol_version": self.config.protocol_version,
                "screen_info": screen_info,
                "screens": self.get_monitor_info(),
            },
            kind=MessageKind.EVENT,
        )
        try:
            self.transport.send_command(ready_env)
        except Exception as e:
            logger.error(f"Failed to send viewer.ready: {e}")

    def _on_transport_event(self, envelope: Envelope) -> None:
        """Invoked by transport background thread; routes to Qt main thread."""
        self.envelope_received_signal.emit(envelope)

    def _handle_envelope_gui_thread(self, envelope: Envelope) -> None:
        """Processes incoming envelope on the Qt GUI main thread."""
        msg_type = envelope.type
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        win_id = envelope.window_id or payload.get("window_id")

        if msg_type == "window.open":
            self._handle_window_open(payload, envelope.message_id)

        elif msg_type == "watchlist.open":
            self._handle_watchlist_open(payload, envelope.message_id)

        elif msg_type == "watchlist.data" and win_id:
            wl_state = self.state_manager.apply_watchlist_snapshot(win_id, payload)
            if win_id in self.watchlists:
                self.watchlists[win_id].set_data(wl_state)
            self._send_ack(envelope.message_id)

        elif msg_type == "snapshot.full" and win_id:
            win_data = self.state_manager.apply_snapshot(win_id, payload)
            if win_id in self.windows:
                self.windows[win_id].bind_data(win_data)
            self._send_ack(envelope.message_id)

        elif msg_type == "bar.append" and win_id:
            bar_data = payload.get("bar", payload)
            self.state_manager.append_bar(win_id, bar_data)
            if win_id in self.windows:
                self.windows[win_id].canvas.mark_layers_dirty()

        elif msg_type == "bar.update" and win_id:
            bar_data = payload.get("bar", payload)
            self.state_manager.update_bar(win_id, bar_data)
            if win_id in self.windows:
                self.windows[win_id].canvas.mark_layers_dirty()

        elif msg_type == "tick.update" and win_id:
            # High-frequency tick -> push to coalescing buffer (fire-and-forget)
            self.coalescer.push_tick(win_id, payload)

        elif msg_type == "overlay.update" and win_id:
            # Overlays update
            win_data = self.state_manager.get_window_data(win_id)
            if win_data and "overlay" in payload:
                ov = payload["overlay"]
                from chart_viewer.models.entities import Overlay
                if isinstance(ov, dict):
                    ov = Overlay(
                        overlay_id=ov["overlay_id"],
                        type=ov["type"],
                        series_id=ov.get("series_id", ""),
                        values=ov.get("values", []),
                        style=ov.get("style", {}),
                        pane=ov.get("pane", "main"),
                        origin=ov.get("origin", "bottom"),
                    )
                win_data.overlays[ov.overlay_id] = ov
                if win_id in self.windows:
                    self.windows[win_id].canvas.mark_layers_dirty()

        elif msg_type == "annotation.set" and win_id:
            self.state_manager.set_annotation(win_id, payload.get("annotation", payload))
            if win_id in self.windows:
                self.windows[win_id].canvas.mark_layers_dirty()

        elif msg_type == "annotation.remove" and win_id:
            ann_id = payload.get("id") or payload.get("annotation_id")
            if ann_id:
                self.state_manager.remove_annotation(win_id, ann_id)
                if win_id in self.windows:
                    self.windows[win_id].canvas.mark_layers_dirty()

        elif msg_type == "topbar.set_block" and win_id:
            self.state_manager.set_topbar_block(win_id, payload)
            if win_id in self.windows:
                self.windows[win_id].topbar.set_block(payload)

        elif msg_type == "layout.restore":
            self._handle_layout_restore(payload)
            self._send_ack(envelope.message_id)

        elif msg_type == "resync.request":
            # Discard local RAM state completely (Section 11)
            self.state_manager.clear_all()
            for win in list(self.windows.values()):
                win.close()
            self.windows.clear()
            self._send_viewer_ready()

        elif msg_type == "window.command" and win_id:
            command = payload.get("command") or payload.get("type")
            if command == "close":
                if win_id in self.windows:
                    self.windows[win_id].close()
                elif win_id in self.watchlists:
                    self.watchlists[win_id].close()

        elif msg_type == "screenshot.request":
            self._handle_screenshot_request(payload)


    def _handle_window_open(self, payload: dict, message_id: bytes) -> None:
        """Create and show a new chart window (Section 8)."""
        win_id = payload.get("window_id")
        if not win_id:
            return

        symbol = payload.get("symbol", "")
        print(f"[CHART] Öffne Chart-Fenster '{win_id}' ({symbol})...", flush=True)

        if win_id not in self.windows:
            win = ChartWindow(window_id=win_id, config=self.config)
            win.symbol = symbol
            self.windows[win_id] = win


            # Register in EventHub
            ws = WindowState(
                window_id=win_id,
                symbol=payload.get("symbol", ""),
                timeframe=Timeframe(unit="D", multiplier=1),
                sync_group_id=payload.get("sync_group_id"),
            )
            self.event_hub.register_window(ws)

            # Connect window signals
            win.window_closed_signal.connect(self._on_window_closed)
            win.geometry_changed_signal.connect(self._on_window_geometry_changed)
            win.flag_changed_signal.connect(self._on_flag_changed)
            win.symbol_change_requested.connect(self._on_symbol_change_requested)
            
            # Register for EventHub symbol routing
            self.event_hub.register_for_flag(win.color_flag, win.request_symbol_change)

            win.canvas.crosshair_moved.connect(
                lambda ts, idx, w_id=win_id: self.event_hub.broadcast_crosshair(w_id, ts, idx)
            )
            win.canvas.annotation_moved.connect(
                lambda ann_id, updated, w_id=win_id: self._send_annotation_moved(w_id, ann_id, updated)
            )
            win.canvas.data_request_more.connect(
                lambda w_id=win_id: self._send_data_request_more(w_id)
            )
            win.canvas.axis_mode_forced.connect(
                lambda mode, w_id=win_id: self._send_axis_mode_forced(w_id, mode)
            )

            # Position if provided
            pos = payload.get("position")
            if pos and "x" in pos and "y" in pos:
                win.move(pos["x"], pos["y"])
            size = payload.get("size")
            if size and "width" in size and "height" in size:
                win.resize(size["width"], size["height"])

            win.show()
        else:
            # Reusing existing window: reposition and resize
            win = self.windows[win_id]
            pos = payload.get("position")
            if pos and "x" in pos and "y" in pos:
                win.move(pos["x"], pos["y"])
            size = payload.get("size")
            if size and "width" in size and "height" in size:
                win.resize(size["width"], size["height"])
            if symbol and symbol != "CHART":
                win.symbol = symbol
            win.show()
            win.raise_()

        self._send_ack(message_id)

    def _handle_watchlist_open(self, payload: dict, message_id: bytes) -> None:
        """Create and show a new watchlist window."""
        win_id = payload.get("window_id") or payload.get("list_id")
        if not win_id:
            return

        print(f"[WATCHLIST] Öffne Watchlist-Fenster '{win_id}'...", flush=True)

        if win_id not in self.watchlists:
            win = WatchlistWindow(window_id=win_id)
            self.watchlists[win_id] = win

            # Connect window signals
            win.window_closed_signal.connect(self._on_watchlist_closed)
            win.geometry_changed_signal.connect(self._on_window_geometry_changed)
            win.flag_changed_signal.connect(self._on_flag_changed)
            win.row_selected_signal.connect(self._on_watchlist_row_selected)

            wl_state = self.state_manager.apply_watchlist_snapshot(win_id, payload)
            win.set_data(wl_state)

            # Position if provided
            pos = payload.get("position")
            if pos and "x" in pos and "y" in pos:
                win.move(pos["x"], pos["y"])
            size = payload.get("size")
            if size and "width" in size and "height" in size:
                win.resize(size["width"], size["height"])

            win.show()
        else:
            # Reusing existing watchlist: reposition and resize
            win = self.watchlists[win_id]
            pos = payload.get("position")
            if pos and "x" in pos and "y" in pos:
                win.move(pos["x"], pos["y"])
            size = payload.get("size")
            if size and "width" in size and "height" in size:
                win.resize(size["width"], size["height"])
            win.show()
            win.raise_()

        self._send_ack(message_id)

    def _on_flag_changed(self, window_id: str, new_flag: int) -> None:
        # Re-register ChartWindow for symbol routing
        if window_id in self.windows:
            win = self.windows[window_id]
            # Since we only track the new flag, we can just unregister all and register
            for flag in range(4):
                self.event_hub.unregister_for_flag(flag, win.request_symbol_change)
            self.event_hub.register_for_flag(new_flag, win.request_symbol_change)

    def _on_watchlist_row_selected(self, window_id: str, symbol: str, color_flag: int) -> None:
        """Watchlist routing symbol to ChartWindows with same flag.
        If no ChartWindow with that flag exists, automatically create one."""
        if self.event_hub.has_listeners_for_flag(color_flag):
            # Existing behavior: route to already open ChartWindow(s) with this flag
            self.event_hub.broadcast_symbol_to_flag(symbol, color_flag)
        else:
            # No ChartWindow with this flag → auto-create one
            chart_win_id = f"win_{symbol.lower()}_1d"
            # Only create if not already open
            if chart_win_id not in self.windows:
                self._handle_window_open({
                    "window_id": chart_win_id,
                    "symbol": symbol,
                }, b"")
                # Apply the watchlist's color_flag to the new window
                if chart_win_id in self.windows:
                    self.windows[chart_win_id].color_flag = color_flag
                    self.windows[chart_win_id].flag_btn.set_flag(color_flag)
                    # Re-register for EventHub symbol routing with the correct flag
                    for flag in range(4):
                        self.event_hub.unregister_for_flag(flag, self.windows[chart_win_id].request_symbol_change)
                    self.event_hub.register_for_flag(color_flag, self.windows[chart_win_id].request_symbol_change)
                    # Request symbol data from server for the new window
                    self._on_symbol_change_requested(chart_win_id, symbol)
            else:
                # Window already exists → just change its symbol
                self.windows[chart_win_id].request_symbol_change(symbol)

    def _on_symbol_change_requested(self, window_id: str, symbol: str) -> None:
        """ChartWindow asking Server for new symbol data."""
        env = make_envelope(
            msg_type="window.change_symbol",
            payload={"window_id": window_id, "symbol": symbol},
            kind=MessageKind.EVENT,
            window_id=window_id,
        )
        try:
            self.transport.send_command(env)
        except Exception as e:
            logger.warning(f"Could not send window.change_symbol: {e}")

    def _handle_layout_restore(self, payload: dict) -> None:
        """Section 11: Rebuild entire window layout pushed by Agent."""
        windows_list = payload.get("windows", [])
        for win_info in windows_list:
            win_id = win_info.get("window_id")
            if not win_id:
                continue
            self._handle_window_open(win_info, b"")
            # Apply snapshot if embedded
            if "bars" in win_info:
                win_data = self.state_manager.apply_snapshot(win_id, win_info)
                if win_id in self.windows:
                    self.windows[win_id].bind_data(win_data)

    def _on_tick_flushed(self, win_id: str, tick_data: dict) -> None:
        """Repaint window upon flushed tick."""
        if win_id in self.windows:
            win_data = self.state_manager.get_window_data(win_id)
            if win_data and win_data.bars:
                # Update close/high/low of current bar
                last_bar = win_data.bars[-1]
                price = float(tick_data.get("price", last_bar.close))
                last_bar.close = price
                last_bar.high = max(last_bar.high, price)
                last_bar.low = min(last_bar.low, price)
                self.windows[win_id].canvas.mark_layers_dirty()

    def _on_crosshair_broadcast_received(self, payload: dict) -> None:
        """Crosshair sync routing through EventHub with downtime clamping (Section 4)."""
        source_id = payload["source_window_id"]
        source_group = payload.get("sync_group_id")
        timestamp = payload["timestamp"]

        for win_id, win in self.windows.items():
            if win_id == source_id:
                continue

            target_ws = self.event_hub.get_window(win_id)
            if not target_ws or target_ws.sync_group_id != source_group:
                # Different sync group -> ignore
                continue

            target_data = self.state_manager.get_window_data(win_id)
            if not target_data or not target_data.bars:
                win.canvas._on_pane_crosshair_moved("main", -1.0, -1.0)
                win.canvas.update()
                continue

            available_ts = target_data.get_bar_timestamps()
            clamped_ts = EventHub.clamp_timestamp_to_available_bars(timestamp, available_ts)

            if clamped_ts is None:
                # Out of loaded range -> crosshair disappears (Section 4)
                win.canvas._on_pane_crosshair_moved("main", -1.0, -1.0)
            else:
                # Calculate pixel X for clamped timestamp
                duration = target_data.timeframe.to_seconds() if target_data.timeframe else 86400
                bar_idx = (clamped_ts - target_data.bars[0].t_open) / duration
                px_x = win.canvas.x_trans.bar_to_x(bar_idx)
                # Keep target Y at center
                curr_y = win.canvas.height() / 2.0
                win.canvas._on_pane_crosshair_moved("main", px_x, curr_y)

            win.canvas.update()

    def _on_window_closed(self, window_id: str) -> None:
        """Informative fire-and-forget event to agent (Section 8)."""
        if window_id in self.windows:
            win = self.windows[window_id]
            for flag in range(4):
                self.event_hub.unregister_for_flag(flag, win.request_symbol_change)
            self.windows.pop(window_id)
            
        self.event_hub.unregister_window(window_id)
        self.state_manager.remove_window(window_id)

        self._send_window_closed(window_id)

    def _on_watchlist_closed(self, window_id: str) -> None:
        """Cleanup Watchlist state and notify Agent."""
        self.watchlists.pop(window_id, None)
        self.state_manager.remove_watchlist(window_id)
        
        self._send_window_closed(window_id)
        
    def _send_window_closed(self, window_id: str) -> None:
        env = make_envelope(
            msg_type="window.closed",
            payload={"window_id": window_id},
            kind=MessageKind.EVENT,
            window_id=window_id,
        )
        try:
            self.transport.send_command(env)
        except Exception as e:
            logger.warning(f"Could not send window.closed: {e}")

    def _on_window_geometry_changed(self, window_id: str, geom: dict) -> None:
        """Informative fire-and-forget event to agent (Section 8)."""
        env = make_envelope(
            msg_type="window.geometry_changed",
            payload={"window_id": window_id, "geometry": geom},
            kind=MessageKind.EVENT,
            window_id=window_id,
        )
        try:
            self.transport.send_command(env)
        except Exception:
            pass

    def _send_annotation_moved(self, window_id: str, annotation_id: str, updated: dict) -> None:
        env = make_envelope(
            msg_type="annotation.moved",
            payload={"annotation_id": annotation_id, "updated": updated},
            kind=MessageKind.EVENT,
            window_id=window_id,
        )
        try:
            self.transport.send_command(env)
        except Exception:
            pass

    def _send_data_request_more(self, window_id: str) -> None:
        env = make_envelope(
            msg_type="data.request_more",
            payload={"window_id": window_id},
            kind=MessageKind.EVENT,
            window_id=window_id,
        )
        try:
            self.transport.send_command(env)
        except Exception:
            pass

    def _send_axis_mode_forced(self, window_id: str, mode: str) -> None:
        env = make_envelope(
            msg_type="axis.mode_forced",
            payload={"window_id": window_id, "forced_mode": mode},
            kind=MessageKind.EVENT,
            window_id=window_id,
        )
        try:
            self.transport.send_command(env)
        except Exception:
            pass

    def _send_ack(self, message_id: bytes) -> None:
        if not message_id:
            return
        ack_env = make_envelope(
            msg_type="ack",
            payload={"status": "ok"},
            kind=MessageKind.ACK,
            message_id=message_id,
        )
        try:
            self.transport.send_command(ack_env)
        except Exception:
            pass

    def _handle_screenshot_request(self, payload: dict) -> None:
        """Capture screenshot of open windows and return base64 images to agent."""
        from chart_viewer.screenshot import ScreenshotCapture
        capturer = ScreenshotCapture(self.config, self.transport, self.windows)
        capturer.handle_request(payload)

    # ── Window Setup Management: Geometry & Monitor Helpers ──────────

    def get_all_geometries(self) -> dict:
        """Collect {window_id: {x, y, width, height}} from all open windows."""
        result = {}
        for win_id, win in self.windows.items():
            result[win_id] = {
                "x": win.x(),
                "y": win.y(),
                "width": win.width(),
                "height": win.height(),
            }
        for wl_id, wl in self.watchlists.items():
            result[wl_id] = {
                "x": wl.x(),
                "y": wl.y(),
                "width": wl.width(),
                "height": wl.height(),
            }
        return result

    def get_monitor_info(self) -> list[dict]:
        """Get information about all connected monitors."""
        screens = QGuiApplication.screens()
        result = []
        primary = QGuiApplication.primaryScreen()
        for i, screen in enumerate(screens):
            geom = screen.geometry()
            result.append({
                "index": i,
                "name": screen.name(),
                "width": geom.width(),
                "height": geom.height(),
                "x": geom.x(),
                "y": geom.y(),
                "is_primary": screen == primary,
                "dpi": int(screen.logicalDotsPerInch()),
            })
        return result

    def reposition_window(self, window_id: str, x: int, y: int, width: int, height: int) -> bool:
        """Move and resize an existing window by ID. Returns True if window found."""
        win = self.windows.get(window_id) or self.watchlists.get(window_id)
        if win:
            win.move(x, y)
            win.resize(width, height)
            return True
        return False

    def close_windows_except(self, keep_ids: set) -> int:
        """Close all windows not in keep_ids. Returns count of closed windows."""
        closed = 0
        for win_id in list(self.windows.keys()):
            if win_id not in keep_ids:
                self.windows[win_id].close()
                closed += 1
        for wl_id in list(self.watchlists.keys()):
            if wl_id not in keep_ids:
                self.watchlists[wl_id].close()
                closed += 1
        return closed

    def _get_windows_on_screen(self, screen) -> list[str]:
        """Return window_ids that are currently on the given QScreen."""
        geom = screen.geometry()
        affected = []
        for win_id, win in self.windows.items():
            cx = win.x() + win.width() // 2
            cy = win.y() + win.height() // 2
            if geom.x() <= cx < geom.x() + geom.width() and geom.y() <= cy < geom.y() + geom.height():
                affected.append(win_id)
        for wl_id, wl in self.watchlists.items():
            cx = wl.x() + wl.width() // 2
            cy = wl.y() + wl.height() // 2
            if geom.x() <= cx < geom.x() + geom.width() and geom.y() <= cy < geom.y() + geom.height():
                affected.append(wl_id)
        return affected

    def _redistribute_windows(self, window_ids: list[str], target_screens) -> None:
        """Redistribute windows across remaining screens with cascade offset."""
        if not target_screens:
            return
        for i, win_id in enumerate(window_ids):
            target = target_screens[i % len(target_screens)]
            geom = target.geometry()
            cascade = (i // len(target_screens)) * 40
            new_x = geom.x() + 50 + cascade
            new_y = geom.y() + 50 + cascade

            win = self.windows.get(win_id) or self.watchlists.get(win_id)
            if win:
                # Keep original size, just reposition
                win.move(max(new_x, geom.x()), max(new_y, geom.y()))
                # Ensure window fits on screen
                if win.x() + win.width() > geom.x() + geom.width():
                    win.resize(geom.width() - 100, win.height())
                if win.y() + win.height() > geom.y() + geom.height():
                    win.resize(win.width(), geom.height() - 100)
                logger.info("Redistributed window '%s' → monitor %d", win_id, i % len(target_screens))

    def _on_screen_added(self, screen) -> None:
        """Monitor connected — log event, notify agent for potential autosave update."""
        logger.info("Monitor connected: %s (%dx%d at %d,%d)",
                     screen.name(), screen.geometry().width(), screen.geometry().height(),
                     screen.geometry().x(), screen.geometry().y())
        self._notify_agent_monitor_change("added", screen)

    def _on_screen_removed(self, screen) -> None:
        """Monitor disconnected (e.g. laptop lid closed) — redistribute windows."""
        logger.warning("Monitor removed: %s", screen.name())

        affected = self._get_windows_on_screen(screen)
        remaining = [s for s in QGuiApplication.screens() if s != screen]

        if affected and remaining:
            logger.info("Redistributing %d windows from removed monitor across %d remaining screens",
                         len(affected), len(remaining))
            self._redistribute_windows(affected, remaining)

        self._notify_agent_monitor_change("removed", screen)

    def _notify_agent_monitor_change(self, change_type: str, screen) -> None:
        """Send monitor change event to the agent so it can trigger autosave."""
        env = make_envelope(
            msg_type="monitor.change",
            payload={
                "change_type": change_type,
                "screen": {
                    "name": screen.name(),
                    "width": screen.geometry().width(),
                    "height": screen.geometry().height(),
                    "x": screen.geometry().x(),
                    "y": screen.geometry().y(),
                },
                "total_screens": len(QGuiApplication.screens()),
                "screens": self.get_monitor_info(),
            },
            kind=MessageKind.EVENT,
        )
        try:
            self.transport.send_command(env)
        except Exception:
            pass
