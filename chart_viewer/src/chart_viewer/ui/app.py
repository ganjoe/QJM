"""ViewerApp managing overall application lifecycle and transport dispatch according to Section 8."""

from __future__ import annotations
import logging
from typing import Dict, Optional
from PySide6.QtCore import QObject, QTimer, Signal, QPointF
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication

from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig
from chart_viewer.transport.base import AgentTransport
from chart_viewer.models.envelope import Envelope, MessageKind, make_envelope
from chart_viewer.core.event_hub import EventHub
from chart_viewer.core.state_manager import StateManager
from chart_viewer.core.backpressure import TickCoalescer
from chart_viewer.ui.window import ChartWindow
from chart_viewer.models.entities import WindowState, Timeframe

logger = logging.getLogger(__name__)


class ViewerApp(QObject):
    """Main application controller binding AgentTransport, EventHub, StateManager, and Windows."""

    # Thread-safe signal to process envelopes on the Qt GUI thread
    envelope_received_signal = Signal(object)

    def __init__(
        self,
        transport: AgentTransport,
        config: ViewerConfig | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.transport = transport
        self.config = config or GLOBAL_CONFIG

        self.event_hub = EventHub()
        self.state_manager = StateManager()
        self.windows: Dict[str, ChartWindow] = {}

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

    def _handle_window_open(self, payload: dict, message_id: bytes) -> None:
        """Create and show a new chart window (Section 8)."""
        win_id = payload.get("window_id")
        if not win_id:
            return

        symbol = payload.get("symbol", "")
        print(f"[CHART] Öffne Chart-Fenster '{win_id}' ({symbol})...", flush=True)

        if win_id not in self.windows:
            win = ChartWindow(window_id=win_id, config=self.config)
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

        self._send_ack(message_id)

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
                win.canvas.layer4_interaction.set_crosshair(None)
                win.canvas.update()
                continue

            available_ts = target_data.get_bar_timestamps()
            clamped_ts = EventHub.clamp_timestamp_to_available_bars(timestamp, available_ts)

            if clamped_ts is None:
                # Out of loaded range -> crosshair disappears (Section 4)
                win.canvas.layer4_interaction.set_crosshair(None)
            else:
                # Calculate pixel X for clamped timestamp
                duration = target_data.timeframe.to_seconds() if target_data.timeframe else 86400
                bar_idx = (clamped_ts - target_data.bars[0].t_open) / duration
                px_x = win.canvas.x_trans.bar_to_x(bar_idx)
                # Keep target Y at current mouse Y or center
                curr_y = win.canvas.layer4_interaction.crosshair_pos.y() if win.canvas.layer4_interaction.crosshair_pos else win.canvas.height() / 2.0
                win.canvas.layer4_interaction.set_crosshair(QPointF(px_x, curr_y))

            win.canvas.update()

    def _on_window_closed(self, window_id: str) -> None:
        """Informative fire-and-forget event to agent (Section 8)."""
        self.windows.pop(window_id, None)
        self.event_hub.unregister_window(window_id)
        self.state_manager.remove_window(window_id)

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
