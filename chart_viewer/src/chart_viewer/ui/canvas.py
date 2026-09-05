"""ChartCanvas widget with multi-pane support via QSplitter according to Section 6.2, 7, & 9."""

from __future__ import annotations
import json
import os
import time
import math
from typing import Callable, Dict, List, Optional
from PySide6.QtCore import Qt, QPointF, Signal, QRectF, QTimer
from PySide6.QtWidgets import QWidget, QSplitter, QVBoxLayout, QApplication
from PySide6.QtGui import QPainter, QPixmap, QMouseEvent, QWheelEvent, QKeyEvent, QFocusEvent, QResizeEvent, QFont, QColor, QPen, QCursor

from chart_viewer.config import ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.core.state_manager import WindowData
from chart_viewer.models.entities import Overlay
from chart_viewer.ui.pane import ChartPane

# Persistence path for splitter heights keyed by pane count
_SPLITTER_PREFS_PATH = os.path.join(os.path.expanduser("~"), ".chart_viewer_splitter.json")

X_AXIS_HEIGHT = 22.0
Y_AXIS_WIDTH = 70.0




class ChartCanvas(QWidget):
    """Multi-pane chart canvas with shared X-axis and per-pane Y-axes."""

    crosshair_moved = Signal(int, float)  # (timestamp, bar_index_fraction)
    annotation_moved = Signal(str, dict)  # (annotation_id, updated_anchors)
    data_request_more = Signal()
    axis_mode_forced = Signal(str)

    def __init__(
        self,
        window_id: str,
        config: ViewerConfig,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.window_id = window_id
        self.config = config

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Shared X-axis transform (same object for all panes)
        self.x_trans = XAxisTransform(config=self.config)

        # Layout
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # Splitter for panes
        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.setHandleWidth(3)
        self._splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #2A2E39;
            }
            QSplitter::handle:hover {
                background-color: #434958;
            }
        """)
        self._layout.addWidget(self._splitter, stretch=1)



        # Pane registry: pane_id → ChartPane
        self._panes: Dict[str, ChartPane] = {}
        self._pane_order: List[str] = []  # Ordered pane IDs (main first)

        # Reference data
        self.window_data: Optional[WindowData] = None

        # Save splitter on change
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        # Keyboard navigation & smooth scrolling state
        self._active_scroll_key: Optional[int] = None
        self._last_scroll_time: float = 0.0
        self._is_smooth_scrolling: bool = False

        self._key_hold_timer = QTimer(self)
        self._key_hold_timer.setSingleShot(True)
        self._key_hold_timer.timeout.connect(self._on_key_hold_timeout)

        scroll_fps = getattr(self.config, "key_scroll_fps", 60)
        scroll_interval_ms = max(5, int(1000 // scroll_fps)) if scroll_fps > 0 else 16
        self._key_scroll_timer = QTimer(self)
        self._key_scroll_timer.setInterval(scroll_interval_ms)
        self._key_scroll_timer.timeout.connect(self._on_smooth_scroll_tick)

        # Keyboard zoom & smooth zooming state
        self._active_zoom_key: Optional[int] = None
        self._last_zoom_time: float = 0.0
        self._is_smooth_zooming: bool = False

        self._key_zoom_hold_timer = QTimer(self)
        self._key_zoom_hold_timer.setSingleShot(True)
        self._key_zoom_hold_timer.timeout.connect(self._on_key_zoom_hold_timeout)

        zoom_fps = getattr(self.config, "key_zoom_fps", 60)
        zoom_interval_ms = max(5, int(1000 // zoom_fps)) if zoom_fps > 0 else 16
        self._key_zoom_timer = QTimer(self)
        self._key_zoom_timer.setInterval(zoom_interval_ms)
        self._key_zoom_timer.timeout.connect(self._on_smooth_zoom_tick)

    def set_window_data(self, win_data: WindowData) -> None:
        """Bind window data and rebuild panes as needed."""
        self.window_data = win_data

        # Determine required panes
        pane_overlays = win_data.get_panes()  # {pane_id: [overlays]}
        required_pane_ids = ["main"]  # Main always exists
        for pane_id in pane_overlays:
            if pane_id != "main" and pane_id not in required_pane_ids:
                required_pane_ids.append(pane_id)

        # Rebuild panes if pane set changed
        if required_pane_ids != self._pane_order:
            self._rebuild_panes(required_pane_ids)

        # Time base
        base_ts = win_data.bars[0].t_open if win_data.bars else 0
        duration = 86400
        if win_data.timeframe:
            duration = win_data.timeframe.to_seconds()

        # Distribute data to panes
        main_pane = self._panes.get("main")
        if main_pane:
            main_overlays = {ov.overlay_id: ov for ov in pane_overlays.get("main", [])}
            main_pane.set_data(win_data.bars, main_overlays, win_data.style_defaults)
            main_pane.watermark_text = f"{win_data.symbol}"
            if win_data.timeframe:
                main_pane.watermark_text += f" • {win_data.timeframe.to_string()}"
            main_pane.base_timestamp = base_ts
            main_pane.bar_duration = duration

        for pane_id, overlays in pane_overlays.items():
            if pane_id == "main":
                continue
            pane = self._panes.get(pane_id)
            if pane:
                ov_dict = {ov.overlay_id: ov for ov in overlays}
                pane.set_data(win_data.bars, ov_dict)
                pane.base_timestamp = base_ts
                pane.bar_duration = duration

        # Init x-axis: latest bar always pinned at 10% right margin
        if win_data.bars:
            latest_idx = float(len(win_data.bars) - 1)
            self.x_trans.latest_bar_index = latest_idx
            self.x_trans.right_index = latest_idx
            self.x_trans.pin_to_right = True
            chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
            if chart_w > 10.0:
                self.x_trans.set_viewport_width(chart_w)
            else:
                self.x_trans.ensure_touches_left()

        # Update all Y ranges
        self._update_all_y_ranges()
        self.mark_layers_dirty()

    def _rebuild_panes(self, pane_ids: List[str]) -> None:
        """Tear down old panes and create new ones for the given pane IDs."""
        # Remove old panes
        for pane_id in list(self._panes.keys()):
            pane = self._panes.pop(pane_id)
            self._splitter.widget(0)  # force layout
            pane.setParent(None)
            pane.deleteLater()

        # Remove all widgets from splitter
        while self._splitter.count() > 0:
            w = self._splitter.widget(0)
            w.setParent(None)

        self._pane_order = pane_ids

        # Create panes
        for i, pane_id in enumerate(pane_ids):
            is_main = (pane_id == "main")
            pane = ChartPane(
                pane_id=pane_id,
                x_trans=self.x_trans,
                config=self.config,
                is_main=is_main,
                parent=self._splitter,
            )
            # Rule: X-axis date/time legend belongs in the pane UNDER the chart (index 1),
            # or in the main chart pane if single pane!
            if len(pane_ids) > 1:
                pane.draw_x_axis = (i == 1)
            else:
                pane.draw_x_axis = is_main

            self._panes[pane_id] = pane
            self._splitter.addWidget(pane)
            stretch = 7 if is_main else 2
            self._splitter.setStretchFactor(i, stretch)
            # Connect crosshair distribution and X-pan
            pane.crosshair_moved_signal.connect(self._on_pane_crosshair_moved)
            pane.x_pan_requested.connect(self._on_x_pan)

        # Restore or set default splitter sizes
        self._restore_splitter_sizes(len(pane_ids))

    def _restore_splitter_sizes(self, pane_count: int) -> None:
        """Load saved splitter sizes for this pane count, or use defaults."""
        try:
            if os.path.exists(_SPLITTER_PREFS_PATH):
                with open(_SPLITTER_PREFS_PATH, "r") as f:
                    prefs = json.load(f)
                key = str(pane_count)
                if key in prefs:
                    self._splitter.setSizes(prefs[key])
                    return
        except Exception:
            pass

        # Default: main gets 70%, rest splits 30% equally
        total = self._splitter.height() or 700
        if pane_count <= 1:
            self._splitter.setSizes([total])
        else:
            main_h = int(total * 0.7)
            rest_h = int((total * 0.3) / (pane_count - 1))
            sizes = [main_h] + [rest_h] * (pane_count - 1)
            self._splitter.setSizes(sizes)

    def _save_splitter_sizes(self) -> None:
        """Persist current splitter sizes keyed by pane count."""
        pane_count = len(self._pane_order)
        if pane_count < 1:
            return
        try:
            prefs = {}
            if os.path.exists(_SPLITTER_PREFS_PATH):
                with open(_SPLITTER_PREFS_PATH, "r") as f:
                    prefs = json.load(f)
            prefs[str(pane_count)] = self._splitter.sizes()
            with open(_SPLITTER_PREFS_PATH, "w") as f:
                json.dump(prefs, f)
        except Exception:
            pass

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        self._save_splitter_sizes()
        # Repaint all panes (they might need new Y-ranges)
        self._update_all_y_ranges()
        self.mark_layers_dirty()

    def _update_all_y_ranges(self) -> None:
        """Update Y-range for all panes."""
        for pane in self._panes.values():
            pane.update_y_range()

    def _on_x_pan(self, delta_px: float) -> None:
        """Handle horizontal X-axis pan from mouse drag (right-click or middle-click)."""
        self.x_trans.pin_to_right = False
        self.x_trans.pan(delta_px)
        self._update_all_y_ranges()
        self.mark_layers_dirty()
        if self.window_data and self.window_data.bars:
            if self.x_trans.should_request_more_data(0.0):
                self.data_request_more.emit()

    def _on_pane_crosshair_moved(self, source_pane_id: str, x_px: float, y_px: float) -> None:
        """Distribute crosshair from one pane to all panes."""
        if x_px < 0:
            # Mouse left the pane — clear all crosshairs
            for pane in self._panes.values():
                pane.set_crosshair(None, None, False)

            return

        # Distribute: vertical line (x) to ALL panes, horizontal (y) only to source
        for pane_id, pane in self._panes.items():
            is_active = (pane_id == source_pane_id)
            pane.set_crosshair(x_px, y_px if is_active else None, is_active)



        # Emit crosshair_moved for inter-window sync
        if self.window_data and self.window_data.bars:
            bar_idx = self.x_trans.x_to_bar(x_px)
            duration = self.window_data.timeframe.to_seconds() if self.window_data.timeframe else 86400
            ts = int(self.window_data.bars[0].t_open + bar_idx * duration)
            self.crosshair_moved.emit(ts, bar_idx)

    def mark_layers_dirty(self) -> None:
        """Mark all panes dirty for repaint."""
        for pane in self._panes.values():
            pane.mark_dirty()


    # ── Delegated interaction (zoom, pan, crosshair) ─────────────────

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
        self.x_trans.set_viewport_width(chart_w)
        self._update_all_y_ranges()
        self.mark_layers_dirty()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Shift+Wheel → delegate to the pane under cursor for Y-zoom
            super().wheelEvent(event)
            return

        # Regular wheel → X-axis zoom (shared)
        angle = event.angleDelta().y()
        factor = 1.15 if angle > 0 else (1.0 / 1.15)
        
        is_pinned = getattr(self.x_trans, "pin_to_right", True)
        if is_pinned:
            # Rule: If at the right edge, X-Zoom ALWAYS enforces the 10% right margin rule
            if self.window_data and self.window_data.bars:
                latest_idx = float(len(self.window_data.bars) - 1)
                self.x_trans.latest_bar_index = latest_idx
                self.x_trans.right_index = latest_idx
            changed = self.x_trans.zoom(factor, pin_to_right=True)
        else:
            # User is panned into history: zoom smoothly around mouse cursor
            changed = self.x_trans.zoom(
                factor,
                anchor_mouse_x=event.position().x(),
                pin_to_right=False,
            )

        if changed:
            self._update_all_y_ranges()
            self.mark_layers_dirty()
            if self.window_data and self.window_data.bars:
                if self.x_trans.should_request_more_data(0.0):
                    self.data_request_more.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._is_panning = True
            self._pan_start = event.position()
            self.x_trans.pin_to_right = False
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if getattr(self, "_is_panning", False):
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self._on_x_pan(delta.x())
            return

        # Data request check
        if self.window_data and self.window_data.bars:
            if self.x_trans.should_request_more_data(0.0):
                self.data_request_more.emit()

        # Crosshair broadcast
        pos = event.position()
        bar_idx = self.x_trans.x_to_bar(pos.x())
        if self.window_data and self.window_data.bars:
            duration = self.window_data.timeframe.to_seconds() if self.window_data.timeframe else 86400
            ts = int(self.window_data.bars[0].t_open + bar_idx * duration)
            self.crosshair_moved.emit(ts, bar_idx)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if getattr(self, "_is_panning", False) and event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._is_panning = False
            if hasattr(self.x_trans, "latest_bar_index") and self.x_trans.latest_bar_index >= 0:
                if self.x_trans.right_index >= self.x_trans.latest_bar_index:
                    self.x_trans.right_index = self.x_trans.latest_bar_index
                    self.x_trans.pin_to_right = True
            return
        super().mouseReleaseEvent(event)

    def _get_zoom_anchor_x(self) -> Optional[float]:
        """Return local mouse X position if mouse is hovering within chart area, else None."""
        try:
            if self.underMouse():
                global_pos = QCursor.pos()
                local_pos = self.mapFromGlobal(global_pos)
                chart_w = max(10.0, self.width() - Y_AXIS_WIDTH)
                if 0.0 <= local_pos.x() <= chart_w:
                    return float(local_pos.x())
        except Exception:
            pass
        return None

    def _apply_zoom(self, factor: float) -> bool:
        """Apply zoom factor to X-axis, respecting pinning and boundaries."""
        is_pinned = getattr(self.x_trans, "pin_to_right", True)
        if is_pinned:
            if self.window_data and self.window_data.bars:
                latest_idx = float(len(self.window_data.bars) - 1)
                self.x_trans.latest_bar_index = latest_idx
                self.x_trans.right_index = latest_idx
            changed = self.x_trans.zoom(factor, pin_to_right=True)
        else:
            anchor_x = self._get_zoom_anchor_x()
            changed = self.x_trans.zoom(factor, anchor_mouse_x=anchor_x, pin_to_right=False)

        if changed:
            self._update_all_y_ranges()
            self.mark_layers_dirty()
            if self.window_data and self.window_data.bars:
                if self.x_trans.should_request_more_data(0.0):
                    self.data_request_more.emit()
        return changed

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            # Reset all pane Y-axes to auto and re-pin right margin
            self.x_trans.pin_to_right = True
            for pane in self._panes.values():
                pane.y_axis_mode = "auto"
                pane.y_trans.reset_boundary()
                pane.update_y_range()
                pane.mark_dirty()
            event.accept()
            return

        is_shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        shift_mult = getattr(self.config, "key_shift_speed_multiplier", 2.0)

        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if event.isAutoRepeat():
                # Ignore OS keyboard repeat; we handle holding via hold timer & smooth scroll timer
                event.accept()
                return

            event.accept()
            self._active_scroll_key = event.key()

            # Immediate single tap: shift by configured step (multiplied if Shift is held)
            base_step = getattr(self.config, "key_scroll_step_bars", 1.0)
            step = (base_step * shift_mult) if is_shift else base_step
            delta = -step if event.key() == Qt.Key.Key_Left else step
            changed = self.x_trans.pan_bars(delta)
            if changed:
                self._update_all_y_ranges()
                self.mark_layers_dirty()
                if self.window_data and self.window_data.bars:
                    if self.x_trans.should_request_more_data(0.0):
                        self.data_request_more.emit()

            if is_shift:
                # Immediate activation without delay when Shift is held
                self._is_smooth_scrolling = True
                self._last_scroll_time = time.perf_counter()
                self._key_scroll_timer.start()
            else:
                # Start hold timer for >500ms smooth scrolling
                self._is_smooth_scrolling = False
                hold_ms = getattr(self.config, "key_scroll_hold_delay_ms", 500)
                self._key_hold_timer.start(hold_ms)
            return

        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            if event.isAutoRepeat():
                event.accept()
                return

            event.accept()
            self._active_zoom_key = event.key()

            # Immediate single tap (multiplied power if Shift is held)
            base_factor = getattr(self.config, "key_zoom_step_factor", 1.15)
            step_factor = (base_factor ** shift_mult) if is_shift else base_factor
            factor = step_factor if event.key() == Qt.Key.Key_Up else (1.0 / step_factor)
            self._apply_zoom(factor)

            if is_shift:
                # Immediate activation without delay when Shift is held
                self._is_smooth_zooming = True
                self._last_zoom_time = time.perf_counter()
                self._key_zoom_timer.start()
            else:
                self._is_smooth_zooming = False
                hold_ms = getattr(self.config, "key_zoom_hold_delay_ms", 500)
                self._key_zoom_hold_timer.start(hold_ms)
            return

        super().keyPressEvent(event)

    def _on_key_hold_timeout(self) -> None:
        """Triggered when key was held down longer than hold threshold (e.g. >500ms)."""
        if self._active_scroll_key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._is_smooth_scrolling = True
            self._last_scroll_time = time.perf_counter()
            self._key_scroll_timer.start()

    def _on_key_zoom_hold_timeout(self) -> None:
        """Triggered when zoom key was held down longer than hold threshold (e.g. >500ms)."""
        if self._active_zoom_key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._is_smooth_zooming = True
            self._last_zoom_time = time.perf_counter()
            self._key_zoom_timer.start()

    def _on_smooth_scroll_tick(self) -> None:
        """Tick event for smooth scrolling at key_scroll_fps (~60 Hz)."""
        if self._active_scroll_key not in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self._stop_key_scrolling()
            return

        now = time.perf_counter()
        dt = now - self._last_scroll_time
        self._last_scroll_time = now

        # Clamp dt in case of system freeze/pause
        dt = min(dt, 0.1)

        is_shift = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        shift_mult = getattr(self.config, "key_shift_speed_multiplier", 2.0) if is_shift else 1.0

        speed = getattr(self.config, "key_scroll_speed_bars_per_sec", 10.0) * shift_mult
        direction = -1.0 if self._active_scroll_key == Qt.Key.Key_Left else 1.0
        delta_bars = direction * speed * dt

        changed = self.x_trans.pan_bars(delta_bars)
        if changed:
            self._update_all_y_ranges()
            self.mark_layers_dirty()
            if self.window_data and self.window_data.bars:
                if self.x_trans.should_request_more_data(0.0):
                    self.data_request_more.emit()

    def _on_smooth_zoom_tick(self) -> None:
        """Tick event for smooth zooming at key_zoom_fps (~60 Hz)."""
        if self._active_zoom_key not in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._stop_key_zooming()
            return

        now = time.perf_counter()
        dt = now - self._last_zoom_time
        self._last_zoom_time = now

        # Clamp dt in case of pause/freeze
        dt = min(dt, 0.1)

        is_shift = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        shift_mult = getattr(self.config, "key_shift_speed_multiplier", 2.0) if is_shift else 1.0

        base_speed = getattr(self.config, "key_zoom_speed_per_sec", 1.15)
        speed = base_speed ** shift_mult
        factor = (speed ** dt) if self._active_zoom_key == Qt.Key.Key_Up else ((1.0 / speed) ** dt)
        self._apply_zoom(factor)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if event.isAutoRepeat():
                event.accept()
                return

            if event.key() == self._active_scroll_key:
                event.accept()
                was_smooth = self._is_smooth_scrolling
                self._stop_key_scrolling()

                # Snap to bar on key release after held scrolling (user preference: "ja snap to bar")
                if was_smooth:
                    snapped = self.x_trans.snap_to_bar()
                    if snapped:
                        self._update_all_y_ranges()
                        self.mark_layers_dirty()
                return

        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            if event.isAutoRepeat():
                event.accept()
                return

            if event.key() == self._active_zoom_key:
                event.accept()
                was_smooth = self._is_smooth_zooming
                self._stop_key_zooming()

                # Snap to bar after held zoom in history
                if was_smooth:
                    snapped = self.x_trans.snap_to_bar()
                    if snapped:
                        self._update_all_y_ranges()
                        self.mark_layers_dirty()
                return

        super().keyReleaseEvent(event)

    def _stop_key_scrolling(self) -> None:
        self._key_hold_timer.stop()
        self._key_scroll_timer.stop()
        self._active_scroll_key = None
        self._is_smooth_scrolling = False

    def _stop_key_zooming(self) -> None:
        self._key_zoom_hold_timer.stop()
        self._key_zoom_timer.stop()
        self._active_zoom_key = None
        self._is_smooth_zooming = False

    def focusOutEvent(self, event: QFocusEvent) -> None:
        if self._active_scroll_key is not None:
            was_smooth = self._is_smooth_scrolling
            self._stop_key_scrolling()
            if was_smooth:
                if self.x_trans.snap_to_bar():
                    self._update_all_y_ranges()
                    self.mark_layers_dirty()

        if self._active_zoom_key is not None:
            was_smooth = self._is_smooth_zooming
            self._stop_key_zooming()
            if was_smooth:
                if self.x_trans.snap_to_bar():
                    self._update_all_y_ranges()
                    self.mark_layers_dirty()

        super().focusOutEvent(event)

