"""ChartCanvas widget with strict 4-layer composition according to Section 6.2, 7, & 9."""

from __future__ import annotations
import time
from typing import Callable, Optional
from PySide6.QtCore import Qt, QPointF, Signal, QRectF
from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QPixmap, QMouseEvent, QWheelEvent, QKeyEvent

from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.coords.y_axis import YAxisTransform
from chart_viewer.core.state_manager import WindowData
from chart_viewer.ui.layers.background import BackgroundLayer
from chart_viewer.ui.layers.data import DataLayer
from chart_viewer.ui.layers.annotations import AnnotationsLayer
from chart_viewer.ui.layers.interaction import InteractionLayer
from chart_viewer.ui.interaction.state_machine import InteractionStateMachine, InteractionState
from chart_viewer.ui.interaction.hit_test import hit_test_annotations
from chart_viewer.ui.context_menu import ChartContextMenu


class ChartCanvas(QWidget):
    """Native Chart Canvas widget implementing 4-layer cached rendering."""

    crosshair_moved = Signal(int, float)  # (timestamp, bar_index_fraction)
    annotation_moved = Signal(str, dict)  # (annotation_id, updated_anchors)
    data_request_more = Signal()
    axis_mode_forced = Signal(str)  # ("log_forced_to_linear")

    def __init__(
        self,
        window_id: str,
        config: ViewerConfig | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.window_id = window_id
        self.config = config or GLOBAL_CONFIG

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Coordinate transforms
        self.x_trans = XAxisTransform(config=self.config)
        self.y_trans = YAxisTransform(config=self.config)

        # Layers 1 - 4
        self.layer1_bg = BackgroundLayer(config=self.config)
        self.layer2_data = DataLayer(config=self.config)
        self.layer3_ann = AnnotationsLayer(config=self.config)
        self.layer4_interaction = InteractionLayer(config=self.config)

        # Cache for Layers 1-3
        self._layers_123_pixmap: Optional[QPixmap] = None
        self._layers_123_dirty: bool = True

        # Interaction State Machine
        self.sm = InteractionStateMachine()
        self._last_mouse_pos = QPointF(0, 0)
        self._drag_start_pos = QPointF(0, 0)

        # Reference data
        self.window_data: Optional[WindowData] = None

    def set_window_data(self, win_data: WindowData) -> None:
        """Bind window data cache and mark cached layers dirty."""
        self.window_data = win_data
        symbol_text = f"{win_data.symbol}"
        if win_data.timeframe:
            symbol_text += f" • {win_data.timeframe.to_string()}"
        self.layer1_bg.set_watermark(symbol_text)

        base_ts = win_data.bars[0].t_open if win_data.bars else 0
        duration = 86400
        if win_data.timeframe:
            duration = win_data.timeframe.to_seconds()

        self.layer1_bg.set_time_base(base_ts, duration)
        self.layer1_bg.set_axis_mode(win_data.y_axis_mode)
        self.layer2_data.set_data(win_data.bars, win_data.overlays, win_data.style_defaults)
        self.layer3_ann.set_annotations(win_data.annotations, base_ts, duration)
        self.layer4_interaction.set_time_base(base_ts, duration)

        if win_data.bars:
            # Set initial right_index if not initialized
            if self.x_trans.right_index == 0.0:
                self.x_trans.right_index = float(len(win_data.bars) - 1)

        self._update_y_range()
        self.mark_layers_dirty()

    def mark_layers_dirty(self) -> None:
        """Flag Layers 1-3 as dirty for next repaint."""
        self._layers_123_dirty = True
        self.update()

    def _update_y_range(self) -> None:
        """Recalculate auto Y-range for visible bars."""
        if not self.window_data or not self.window_data.bars:
            return

        if self.window_data.y_axis_mode == "manual":
            return

        bars = self.window_data.bars
        min_idx = max(0, int(self.x_trans.min_visible_bar_index))
        max_idx = min(len(bars) - 1, int(self.x_trans.right_index))

        if min_idx > max_idx:
            return

        visible_slice = bars[min_idx : max_idx + 1]
        p_min = min(b.low for b in visible_slice)
        p_max = max(b.high for b in visible_slice)

        req_mode = "log" if self.window_data.y_axis_mode == "log" else "linear"
        forced = self.y_trans.fit_range(p_min, p_max, requested_mode=req_mode)

        if forced:
            self.axis_mode_forced.emit("log_forced_to_linear")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        w = max(1.0, float(self.width()))
        h = max(1.0, float(self.height()))
        chart_w = max(1.0, w - 70.0)
        chart_h = max(1.0, h - 22.0)
        self.x_trans.viewport_width_px = chart_w
        self.y_trans.viewport_height_px = chart_h
        self._update_y_range()
        self.mark_layers_dirty()

    def paintEvent(self, event) -> None:
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        # Section 6.2: Ensure Layers 1-3 are cached in offscreen QPixmap
        if self._layers_123_dirty or self._layers_123_pixmap is None or self._layers_123_pixmap.size() != self.size():
            pixmap = QPixmap(self.size())
            pix_painter = QPainter(pixmap)
            try:
                pix_painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

                # Draw Layer 1 (Background)
                self.layer1_bg.render(pix_painter, self.x_trans, self.y_trans, w, h)
                # Draw Layer 2 (Data)
                self.layer2_data.render(pix_painter, self.x_trans, self.y_trans, w, h)
                # Draw Layer 3 (Annotations)
                self.layer3_ann.render(pix_painter, self.x_trans, self.y_trans, w, h)
            except Exception as e:
                import logging
                logging.getLogger("ChartCanvas").exception(f"Error rendering chart layers: {e}")
            finally:
                pix_painter.end()

            self._layers_123_pixmap = pixmap
            self._layers_123_dirty = False

        # Blit cached Layers 1-3 to screen
        screen_painter = QPainter(self)
        try:
            screen_painter.drawPixmap(0, 0, self._layers_123_pixmap)

            # Draw Layer 4 (Interaction) live on top (sub-millisecond!)
            self.layer4_interaction.render(screen_painter, self.x_trans, self.y_trans, w, h)
        finally:
            screen_painter.end()

    # --- Mouse & Interaction Events (Section 7) ---

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        delta = pos - self._last_mouse_pos
        self._last_mouse_pos = pos

        chart_w = max(1.0, float(self.width()) - 70.0)
        chart_h = max(1.0, float(self.height()) - 22.0)

        # Update Crosshair position
        self.layer4_interaction.set_crosshair(pos)

        # State machine dispatch for active dragging
        if self.sm.state == InteractionState.SCALING_Y:
            # Dragging on Y-axis scale (Section 7)
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            dy = pos.y() - self._drag_start_pos.y()
            self._drag_start_pos = pos
            scale_factor = 1.0 - (dy * 0.008)
            if scale_factor > 0:
                self.y_trans.manual_scale(scale_factor)
                self.mark_layers_dirty()
            return

        elif self.sm.state == InteractionState.PANNING:
            self.x_trans.pan(delta.x())
            self._update_y_range()
            self.mark_layers_dirty()
            return

        elif self.sm.state == InteractionState.MEASURING:
            # Only Layer 4 repainted during measuring!
            self.update()
            return

        elif self.sm.state == InteractionState.DRAGGING_ANNOTATION:
            # Dragging annotation: optimistic update
            ann_id = self.sm.active_annotation_id
            if ann_id and self.window_data and ann_id in self.window_data.annotations:
                ann = self.window_data.annotations[ann_id]
                anchor_idx = self.sm.active_anchor_index
                new_price = self.y_trans.y_to_price(pos.y())
                bar_idx = self.x_trans.x_to_bar(pos.x())
                duration = self.window_data.timeframe.to_seconds() if self.window_data.timeframe else 86400
                new_t = int(self.window_data.bars[0].t_open + bar_idx * duration) if self.window_data.bars else 0

                if anchor_idx is not None and anchor_idx < len(ann.anchors):
                    ann.anchors[anchor_idx].price = new_price
                    ann.anchors[anchor_idx].t = new_t
                else:
                    # Move entire annotation
                    for anchor in ann.anchors:
                        anchor.price = new_price
                self.mark_layers_dirty()
            return

        # Idle hover: cursor selection
        if pos.x() >= chart_w and pos.y() < chart_h:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

        # Check edge data request
        if self.window_data and self.window_data.bars:
            if self.x_trans.should_request_more_data(0.0):
                self.data_request_more.emit()

        # Emit Crosshair Broadcast
        bar_idx = self.x_trans.x_to_bar(pos.x())
        if self.window_data and self.window_data.bars:
            duration = self.window_data.timeframe.to_seconds() if self.window_data.timeframe else 86400
            ts = int(self.window_data.bars[0].t_open + bar_idx * duration)
            self.crosshair_moved.emit(ts, bar_idx)

        # Repaint Layer 4 (Crosshair)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        self._drag_start_pos = pos

        chart_w = max(1.0, float(self.width()) - 70.0)
        chart_h = max(1.0, float(self.height()) - 22.0)

        if event.button() == Qt.MouseButton.MiddleButton:
            # Middle click + drag -> Panning (Section 7)
            self.sm.start_panning()

        elif event.button() == Qt.MouseButton.LeftButton:
            if pos.x() >= chart_w and pos.y() < chart_h:
                # Drag on Y-axis -> Manual Y-scaling, sets y_axis_mode = manual (Section 7)
                if self.window_data:
                    self.window_data.y_axis_mode = "manual"
                    self.layer1_bg.set_axis_mode("manual")
                self.sm.start_scaling_y()
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                self.mark_layers_dirty()
                return

            # Hit-test annotations (handles -> lines -> empty space)
            base_ts = self.window_data.bars[0].t_open if (self.window_data and self.window_data.bars) else 0
            duration = self.window_data.timeframe.to_seconds() if (self.window_data and self.window_data.timeframe) else 86400
            ann_id, anchor_idx = hit_test_annotations(
                pos,
                self.window_data.annotations if self.window_data else {},
                self.x_trans,
                self.y_trans,
                base_ts,
                duration,
                radius=self.config.hit_test_radius_px,
            )

            if ann_id:
                # Dragging annotation (Section 7)
                self.layer3_ann.selected_annotation_id = ann_id
                self.sm.start_dragging_annotation(ann_id, anchor_idx)
                self.mark_layers_dirty()
            else:
                # Left click on empty chart area -> Measuring Tool (Section 7)
                if pos.x() < chart_w and pos.y() < chart_h:
                    self.layer3_ann.selected_annotation_id = None
                    self.sm.start_measuring()
                    price = self.y_trans.y_to_price(pos.y())
                    bar_idx = self.x_trans.x_to_bar(pos.x())
                    ts = int(base_ts + bar_idx * duration)
                    self.layer4_interaction.start_measuring(pos, price, bar_idx, ts)
                    self.mark_layers_dirty()

        elif event.button() == Qt.MouseButton.RightButton:
            # Right-click context menu (Section 7)
            base_ts = self.window_data.bars[0].t_open if (self.window_data and self.window_data.bars) else 0
            duration = self.window_data.timeframe.to_seconds() if (self.window_data and self.window_data.timeframe) else 86400
            ann_id, _ = hit_test_annotations(
                pos,
                self.window_data.annotations if self.window_data else {},
                self.x_trans,
                self.y_trans,
                base_ts,
                duration,
                radius=self.config.hit_test_radius_px,
            )
            ChartContextMenu.show_menu(
                parent=self,
                global_pos=event.globalPosition().toPoint(),
                hit_annotation_id=ann_id,
                on_delete_annotation=self._on_delete_annotation,
                on_reset_axes=self._on_reset_axes,
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        prior_state, ann_id = self.sm.release()

        if prior_state == InteractionState.MEASURING:
            # Discard measurement immediately without persistence (Section 7)
            self.layer4_interaction.stop_measuring()
            self.update()

        elif prior_state == InteractionState.DRAGGING_ANNOTATION and ann_id:
            # Send optimistic annotation.moved message to agent (Section 7)
            if self.window_data and ann_id in self.window_data.annotations:
                ann = self.window_data.annotations[ann_id]
                anchors_payload = [
                    {"t": a.t, "price": a.price, "x_px": a.x_px, "y_px": a.y_px, "mode": a.mode}
                    for a in ann.anchors
                ]
                self.annotation_moved.emit(ann_id, {"anchors": anchors_payload})

        elif prior_state == InteractionState.SCALING_Y:
            chart_w = max(1.0, float(self.width()) - 70.0)
            if event.position().x() < chart_w:
                self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        chart_w = max(1.0, float(self.width()) - 70.0)
        if pos.x() >= chart_w:
            # Double-click on Y-axis -> Reset axes to Auto!
            self._on_reset_axes()
        else:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        modifiers = event.modifiers()
        angle = event.angleDelta().y()
        factor = 1.15 if angle > 0 else (1.0 / 1.15)
        mouse_pos = event.position()

        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            # Shift + Wheel -> Manual Y-Zoom, sets y_axis_mode = manual (Section 7)
            if self.window_data:
                self.window_data.y_axis_mode = "manual"
                self.layer1_bg.set_axis_mode("manual")
            self.y_trans.manual_scale(factor)
            self.mark_layers_dirty()
        else:
            # Wheel -> X-Zoom with fixed cursor anchor (Section 7)
            changed = self.x_trans.zoom(factor, anchor_mouse_x=mouse_pos.x())
            if changed:
                self._update_y_range()
                self.mark_layers_dirty()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            # Esc -> Cancel active tool/drag, discard overlay (highest priority, Section 7)
            self.sm.cancel()
            self.layer4_interaction.stop_measuring()
            self.layer3_ann.selected_annotation_id = None
            self.mark_layers_dirty()
        else:
            super().keyPressEvent(event)

    def _on_delete_annotation(self, ann_id: str) -> None:
        if self.window_data and ann_id in self.window_data.annotations:
            self.window_data.annotations.pop(ann_id, None)
            self.mark_layers_dirty()

    def _on_reset_axes(self) -> None:
        if self.window_data:
            self.window_data.y_axis_mode = "auto"
        self.layer1_bg.set_axis_mode("auto")
        self._update_y_range()
        self.mark_layers_dirty()

