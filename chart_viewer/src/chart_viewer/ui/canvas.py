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
from chart_viewer.models.validation import sanitize_pane_scales
from chart_viewer.ui.pane import ChartPane

# Persistence path for splitter heights keyed by pane count
_SPLITTER_PREFS_PATH = os.path.join(os.path.expanduser("~"), ".chart_viewer_splitter.json")

X_AXIS_HEIGHT = 22.0
Y_AXIS_WIDTH = 70.0




class ChartCanvas(QWidget):
    """Multi-pane chart canvas with shared X-axis and per-pane Y-axes."""

    crosshair_moved = Signal(int, int)  # (timestamp, bar_index)
    annotation_moved = Signal(str, dict)  # (annotation_id, updated_anchors)
    data_request_more = Signal()
    axis_mode_forced = Signal(str)
    pane_scale_changed = Signal(str, str)  # (pane_id, "linear"|"log") user clicked the LOG/LIN button

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
        self._pane_order: List[str] = []  # Ordered pane IDs (price pane first)
        # (pane_id, weight) signature of the last applied splitter layout, so a
        # live snapshot does not reset a drag in progress.
        self._applied_weight_signature: Optional[tuple] = None

        # Reference data
        self.window_data: Optional[WindowData] = None

        # Window focus: an unfocused chart window shows the idle value boxes
        # (newest value of every curve) instead of a frozen crosshair.
        self._window_active: bool = True

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
        """Bind window data and adjust the panes (diff, no blind rebuild)."""
        self.window_data = win_data

        pane_overlays = win_data.get_panes()  # {pane_id: [overlays]}

        # Chart definition (contract section 3): the "panes" list is the source
        # of order/role/title/weight. Old snapshots without it keep the previous
        # overlay-derived layout.
        specs = self._resolve_pane_specs(win_data, pane_overlays)
        structure_changed = self._sync_panes(specs)
        self._apply_x_axis_pane(specs, getattr(win_data, "x_axis_pane", None))
        self._apply_splitter_weights(specs, structure_changed)

        # Time base
        base_ts = win_data.bars[0].t_open if win_data.bars else 0
        duration = 86400
        if win_data.timeframe:
            duration = win_data.timeframe.to_seconds()

        # Distribute data to panes (price pane: candles + annotations + watermark)
        price_pane = self._price_pane()
        if price_pane is not None:
            price_overlays = {
                ov.overlay_id: ov for ov in pane_overlays.get(price_pane.pane_id, [])
            }
            price_pane.set_data(win_data.bars, price_overlays, win_data.style_defaults)
            price_pane.set_annotations(win_data.annotations)
            price_pane.watermark_text = f"{win_data.symbol}"
            if win_data.timeframe:
                price_pane.watermark_text += f" • {win_data.timeframe.to_string()}"
            price_pane.base_timestamp = base_ts
            price_pane.bar_duration = duration

        for spec in specs:
            pane_id = spec["pane_id"]
            if price_pane is not None and pane_id == price_pane.pane_id:
                continue
            pane = self._panes.get(pane_id)
            if pane:
                ov_dict = {ov.overlay_id: ov for ov in pane_overlays.get(pane_id, [])}
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
        # Preset/snapshot Y-scales (linear/log per pane) belong to the new data
        self.apply_pane_scales(getattr(win_data, "pane_scales", None))
        self.mark_layers_dirty()

    # ── Pane layout (chart definition) ────────────────────

    def _resolve_pane_specs(self, win_data: WindowData, pane_overlays) -> List[dict]:
        """Ordered pane specs of a window (contract docs/architecture/chart-presets.md 3).

        A snapshot carrying "panes" defines order, role, title and weight
        explicitly. Without it the previous behaviour applies: "main" first,
        then every overlay pane in appearance order.
        """
        specs: List[dict] = []
        seen = set()
        for index, entry in enumerate(getattr(win_data, "panes", None) or []):
            if isinstance(entry, str):
                pane_id, role, title, weight = entry, "", "", None
            elif isinstance(entry, dict):
                pane_id = entry.get("pane_id") or entry.get("preset_id") or f"pane_{index}"
                role = str(entry.get("role") or "").strip().lower()
                title = entry.get("title") or ""
                weight = entry.get("weight")
            else:
                continue
            pane_id = str(pane_id).strip()
            if not pane_id or pane_id in seen:
                continue
            seen.add(pane_id)
            specs.append({
                "pane_id": pane_id,
                "role": role if role in ("price", "value", "volume") else "",
                "title": str(title),
                "weight": self._coerce_weight(weight),
                # Feste Y-Spanne je Pane (Pane-Preset "params.range"): vom
                # Server validiert, hier unveraendert durchgereicht.
                "range": entry.get("range") if isinstance(entry, dict) else None,
            })

        if not specs:
            # Old snapshot: derive the panes from the overlays as before.
            pane_ids = ["main"]
            for pane_id in pane_overlays:
                normalized = str(pane_id or "main")
                if normalized not in pane_ids:
                    pane_ids.append(normalized)
            specs = [
                {"pane_id": pane_id, "role": "", "title": "", "weight": None}
                for pane_id in pane_ids
            ]

        return self._finalize_pane_specs(specs)

    @staticmethod
    def _coerce_weight(weight) -> Optional[int]:
        """Pane weight from the snapshot; None when the snapshot carries none."""
        if weight is None:
            return None
        try:
            value = int(weight)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _finalize_pane_specs(specs: List[dict]) -> List[dict]:
        """Assign the single price pane and the effective weights.

        Contract: exactly one pane has role=price (and the id "main"); without
        one the "main" pane, otherwise the first pane becomes the price pane
        (compatibility with old data). Panes without an explicit weight keep the
        historical 7 (price) / 2 (others) stretch.
        """
        main_index = None
        for index, spec in enumerate(specs):
            if spec["role"] == "price":
                main_index = index
                break
        if main_index is None:
            for index, spec in enumerate(specs):
                if spec["pane_id"] == "main":
                    main_index = index
                    break
        if main_index is None:
            main_index = 0
        for index, spec in enumerate(specs):
            spec["is_main"] = (index == main_index)
            if not spec["role"]:
                spec["role"] = "price" if index == main_index else "value"
            spec["has_weight"] = spec["weight"] is not None
            if spec["weight"] is None:
                spec["weight"] = 7 if index == main_index else 2
        return specs

    def _sync_panes(self, specs: List[dict]) -> bool:
        """Create/move/remove pane widgets so the splitter matches "specs".

        Panes are keyed by pane_id: an existing widget is reused (crosshair,
        Y-scale and signal connections survive), only new ids are created and
        vanished ids are deleted. Returns True when the pane set or order
        changed, so the caller only re-lays out the splitter when needed.
        """
        desired = [spec["pane_id"] for spec in specs]

        for pane_id in list(self._panes.keys()):
            if pane_id not in desired:
                pane = self._panes.pop(pane_id)
                pane.setParent(None)
                pane.deleteLater()

        for index, spec in enumerate(specs):
            pane = self._panes.get(spec["pane_id"])
            if pane is None:
                pane = ChartPane(
                    pane_id=spec["pane_id"],
                    x_trans=self.x_trans,
                    config=self.config,
                    is_main=spec["is_main"],
                    role=spec["role"],
                    title=spec["title"],
                    weight=spec["weight"],
                    parent=self._splitter,
                )
                # Connect crosshair distribution and X-pan
                pane.crosshair_moved_signal.connect(self._on_pane_crosshair_moved)
                pane.x_pan_requested.connect(self._on_x_pan)
                pane.y_scale_type_changed.connect(self._on_pane_scale_type_changed)
                self._panes[spec["pane_id"]] = pane
            else:
                pane.is_main = spec["is_main"]
                pane.role = spec["role"]
                if pane.title != spec["title"]:
                    pane.title = spec["title"]
                    pane.mark_dirty()
                pane.weight = spec["weight"]
            pane.set_fixed_range(spec.get("range"))
            # insertWidget MOVES an existing widget to the new position instead
            # of rebuilding it — a reorder never deletes the running panes.
            self._splitter.insertWidget(index, pane)

        changed = desired != self._pane_order
        self._pane_order = desired
        return changed

    def _apply_x_axis_pane(self, specs: List[dict], x_axis_pane) -> None:
        """Exactly one pane draws the X-axis: the configured pane, else the bottom one."""
        pane_ids = [spec["pane_id"] for spec in specs]
        target = str(x_axis_pane) if x_axis_pane else ""
        if target not in pane_ids:
            target = pane_ids[-1] if pane_ids else ""
        for pane_id in pane_ids:
            pane = self._panes.get(pane_id)
            if pane is not None:
                pane.draw_x_axis = (pane_id == target)

    def _apply_splitter_weights(self, specs: List[dict], structure_changed: bool) -> None:
        """Give every pane its stretch factor and split height.

        Weights from the snapshot win. Without them (old snapshots) the local
        splitter prefs file fallback applies, as before. Sizes are recomputed
        only when the layout actually changed, so a user's running splitter drag
        survives live snapshot updates.
        """
        signature = tuple((spec["pane_id"], spec["weight"]) for spec in specs)
        if not structure_changed and signature == self._applied_weight_signature:
            return

        for index, spec in enumerate(specs):
            self._splitter.setStretchFactor(index, max(1, int(spec["weight"])))

        if any(spec["has_weight"] for spec in specs):
            total = self._splitter.height() or 700
            weights = [max(1, int(spec["weight"])) for spec in specs]
            weight_sum = sum(weights) or 1
            self._splitter.setSizes([max(1, int(total * w / weight_sum)) for w in weights])
        else:
            self._restore_splitter_sizes(len(specs))

        self._applied_weight_signature = signature

    def _rebuild_panes(self, pane_ids: List[str]) -> None:
        """Compatibility entry point: sync the panes from a plain list of pane IDs."""
        specs = self._finalize_pane_specs([
            {"pane_id": str(pane_id), "role": "", "title": "", "weight": None}
            for pane_id in pane_ids
        ])
        structure_changed = self._sync_panes(specs)
        self._apply_x_axis_pane(specs, None)
        self._apply_splitter_weights(specs, structure_changed)
        self.mark_layers_dirty()

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

    def _price_pane(self) -> Optional[ChartPane]:
        """The pane with role=price; falls back to the legacy id "main"."""
        for pane in self._panes.values():
            if pane.is_main:
                return pane
        return self._panes.get("main")

    # -- Y-scale per pane (linear/log) ------------------------------------

    def _on_pane_scale_type_changed(self, pane_id: str, scale_type: str) -> None:
        """A pane LOG/LIN button was clicked -> tell the container (window)."""
        self.mark_layers_dirty()
        self.pane_scale_changed.emit(pane_id, scale_type)

    def apply_pane_scales(self, pane_scales) -> None:
        """Apply a {pane_id: "linear"|"log"} mapping to the existing panes.

        Panes missing from the mapping fall back to linear (viewer default).
        The change is applied without notifying the agent: a preset/snapshot is
        the source, not a user click, and an echo would rewrite the preset.
        """
        clean = sanitize_pane_scales(pane_scales)
        for pane_id, pane in self._panes.items():
            pane.set_y_scale_type(clean.get(pane_id, "linear"), refit=False, notify=False)
        self._update_all_y_ranges()
        self.mark_layers_dirty()

    def pane_scales(self) -> Dict[str, str]:
        """Current Y-scale of every pane, e.g. for persisting it in a preset."""
        return {pane_id: pane.y_scale_type for pane_id, pane in self._panes.items()}

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
        """Slot for user mouse moves: render locally and broadcast inter-window."""
        self._apply_crosshair(source_pane_id, x_px, y_px)
        if x_px < 0:
            return
        self._broadcast_crosshair_from_x(x_px)

    def _snap_crosshair(self, x_px: float) -> tuple[Optional[int], float]:
        """Resolve a pixel X to (bar_index, snapped_x) using nearest-neighbour.

        Returns (None, x_px) when no bars are loaded. The snapped x is the real
        candle center, so the vertical line and the date badge always agree.
        """
        bars = self.window_data.bars if self.window_data else []
        if not bars:
            return None, x_px
        idx, snapped_x = self.x_trans.bar_center_for_pixel(x_px, len(bars))
        if not getattr(self.config, "crosshair_snap_to_bar", True):
            return idx, x_px
        return idx, snapped_x

    def _broadcast_crosshair_from_x(self, x_px: float) -> None:
        """Snap the cursor to the nearest real bar and emit its timestamp + index.

        Uses the actual bar t_open (never a nominal duration), so the emitted timestamp
        is a real bar timestamp regardless of downtime/weekend gaps or unit scale.
        """
        if not self.window_data or not self.window_data.bars:
            return
        idx, _ = self._snap_crosshair(x_px)
        if idx is None:
            return
        ts = self.window_data.bars[idx].t_open
        self.crosshair_moved.emit(ts, idx)

    def _apply_crosshair(self, source_pane_id: str, x_px: float, y_px: float) -> None:
        """Render the crosshair locally WITHOUT re-broadcasting (mouse path)."""
        if x_px < 0 or not self.window_data or not self.window_data.bars:
            # Mouse left the pane, or no bars: clear all crosshairs
            for pane in self._panes.values():
                pane.set_crosshair(None, None, False)
            return

        idx, snapped_x = self._snap_crosshair(x_px)

        # Distribute: vertical line (x) to ALL panes, horizontal (y) only to source
        for pane_id, pane in self._panes.items():
            is_active = (pane_id == source_pane_id)
            pane.set_crosshair(snapped_x, y_px if is_active else None, is_active, idx)

    def _apply_crosshair_remote(self, x_px: Optional[float], bar_index: Optional[int] = None) -> None:
        """Apply an inter-window sync: vertical line only, no active pane, no Y."""
        for pane in self._panes.values():
            pane.set_crosshair(x_px, None, False, bar_index)

    def set_window_active(self, active: bool) -> None:
        """Report that this chart window gained or lost the focus.

        Losing the focus (Control Panel, another application) drops the
        crosshair: the pointer is no longer tracking this chart, so every pane
        falls back to its idle readout — the value boxes then show the newest
        value of each curve instead of the last hovered bar.
        """
        self._window_active = bool(active)
        if not self._window_active:
            for pane in self._panes.values():
                pane.set_crosshair(None, None, False)

    def set_annotations(self, annotations) -> None:
        """Push the annotation set to the chart pane without touching zoom/pan state.

        Live annotation.set / annotation.remove messages must not reinitialise the
        viewport the way set_window_data() does (that pins the X-axis to the right
        edge and would jerk the chart on every drawing update).
        """
        price_pane = self._price_pane()
        if price_pane:
            price_pane.set_annotations(annotations)

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
        if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) and not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            # Shift+Wheel → delegate to the pane under cursor for Y-zoom
            super().wheelEvent(event)
            return

        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+Wheel → horizontal scroll (pan along X-axis)
            angle = event.angleDelta().y()
            if angle == 0:
                angle = event.angleDelta().x()
            if angle != 0:
                notches = angle / 120.0
                bars_per_notch = getattr(self.config, "wheel_scroll_step_bars", 5.0)
                # Rolling wheel UP (angle > 0) -> scroll to the left (history / back in time): delta < 0
                # Rolling wheel DOWN (angle < 0) -> scroll to the right (future / latest bar): delta > 0
                delta = -notches * bars_per_notch
                changed = self.x_trans.pan_bars(delta)
                if changed:
                    self._update_all_y_ranges()
                    self.mark_layers_dirty()
                    if self.window_data and self.window_data.bars:
                        if self.x_trans.should_request_more_data(0.0):
                            self.data_request_more.emit()
            event.accept()
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

        # Crosshair broadcast (single path, index-based)
        self._broadcast_crosshair_from_x(event.position().x())

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

