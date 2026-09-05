"""In-memory stateless data manager according to Section 8, 10, & 11."""

from __future__ import annotations
import logging
from typing import Dict, List, Optional
from chart_viewer.models.entities import (
    Bar,
    Overlay,
    OverlayPoint,
    Annotation,
    Anchor,
    TopBarBlock,
    WindowState,
    Instrument,
    Timeframe,
    Calendar,
)
from chart_viewer.models.validation import validate_bar

logger = logging.getLogger(__name__)


class WindowData:
    """RAM-only data cache for a single window."""

    def __init__(self, window_id: str):
        self.window_id = window_id
        self.symbol: str = ""
        self.timeframe: Optional[Timeframe] = None
        self.instrument: Optional[Instrument] = None
        self.bars: List[Bar] = []
        self.overlays: Dict[str, Overlay] = {}
        self.annotations: Dict[str, Annotation] = {}
        self.topbar_blocks: Dict[str, TopBarBlock] = {}
        self.y_axis_mode: str = "auto"
        self.sync_group_id: Optional[str] = None
        self.style_defaults: dict = {}

    def get_bar_timestamps(self) -> List[int]:
        return [b.t_open for b in self.bars]

    def get_panes(self) -> Dict[str, List[Overlay]]:
        """Group overlays by their pane field. Returns {pane_id: [overlays]}."""
        panes: Dict[str, List[Overlay]] = {}
        for ov in self.overlays.values():
            pane_id = ov.pane or "main"
            panes.setdefault(pane_id, []).append(ov)
        return panes


class StateManager:
    """Stateless in-memory store for the Viewer process."""

    def __init__(self):
        self._windows: Dict[str, WindowData] = {}

    def get_window_data(self, window_id: str) -> Optional[WindowData]:
        return self._windows.get(window_id)

    def get_or_create_window_data(self, window_id: str) -> WindowData:
        if window_id not in self._windows:
            self._windows[window_id] = WindowData(window_id)
        return self._windows[window_id]

    def remove_window(self, window_id: str) -> None:
        self._windows.pop(window_id, None)

    def clear_all(self) -> None:
        """Full state reset upon reconnect or sequence gap (Section 11)."""
        logger.info("StateManager: Performing full state reset (discarding all RAM state)")
        self._windows.clear()

    def apply_snapshot(self, window_id: str, payload: dict) -> WindowData:
        """Apply snapshot.full, replacing all bars, overlays, annotations."""
        win_data = self.get_or_create_window_data(window_id)

        # Parse symbol, timeframe, instrument
        if "symbol" in payload:
            win_data.symbol = payload["symbol"]
        if "timeframe" in payload and payload["timeframe"]:
            tf_raw = payload["timeframe"]
            if isinstance(tf_raw, Timeframe):
                win_data.timeframe = tf_raw
            elif isinstance(tf_raw, dict):
                win_data.timeframe = Timeframe(unit=tf_raw.get("unit", "D"), multiplier=int(tf_raw.get("multiplier", 1)))
            elif isinstance(tf_raw, str):
                win_data.timeframe = Timeframe.from_string(tf_raw)
        if "y_axis_mode" in payload:
            win_data.y_axis_mode = payload["y_axis_mode"]
        if "sync_group_id" in payload:
            win_data.sync_group_id = payload["sync_group_id"]
        if "style_defaults" in payload:
            win_data.style_defaults = payload["style_defaults"]

        # Parse bars
        raw_bars = payload.get("bars", [])
        bars = []
        for b in raw_bars:
            if isinstance(b, Bar):
                bar_obj = validate_bar(b)
            elif isinstance(b, dict):
                bar_obj = validate_bar(
                    Bar(
                        t_open=int(b["t_open"]),
                        t_close=int(b["t_close"]),
                        open=float(b["open"]),
                        high=float(b["high"]),
                        low=float(b["low"]),
                        close=float(b["close"]),
                        volume=float(b.get("volume", 0.0)),
                        color_override=b.get("color_override"),
                        fill_override=b.get("fill_override"),
                    )
                )
            bars.append(bar_obj)
        win_data.bars = bars

        # Parse overlays
        raw_overlays = payload.get("overlays", [])
        win_data.overlays.clear()
        for ov in raw_overlays:
            if isinstance(ov, Overlay):
                win_data.overlays[ov.overlay_id] = ov
            elif isinstance(ov, dict):
                raw_values = ov.get("values", [])
                values = []
                for val in raw_values:
                    if isinstance(val, OverlayPoint):
                        values.append(val)
                    elif isinstance(val, dict):
                        values.append(
                            OverlayPoint(
                                t=int(val["t"]),
                                value=float(val["value"]),
                                value2=float(val["value2"]) if val.get("value2") is not None else None,
                            )
                        )
                ov_obj = Overlay(
                    overlay_id=ov["overlay_id"],
                    type=ov["type"],
                    series_id=ov.get("series_id", ""),
                    values=values,
                    style=ov.get("style", {}),
                    pane=ov.get("pane", "main"),
                    origin=ov.get("origin", "bottom"),
                )
                win_data.overlays[ov_obj.overlay_id] = ov_obj

        # Parse annotations
        raw_annotations = payload.get("annotations", [])
        win_data.annotations.clear()
        for ann in raw_annotations:
            if isinstance(ann, Annotation):
                win_data.annotations[ann.id] = ann
            elif isinstance(ann, dict):
                raw_anchors = ann.get("anchors", [])
                anchors = []
                for a in raw_anchors:
                    if isinstance(a, Anchor):
                        anchors.append(a)
                    elif isinstance(a, dict):
                        anchors.append(
                            Anchor(
                                t=a.get("t"),
                                price=float(a["price"]) if a.get("price") is not None else None,
                                x_px=float(a["x_px"]) if a.get("x_px") is not None else None,
                                y_px=float(a["y_px"]) if a.get("y_px") is not None else None,
                                mode=a.get("mode", "data"),
                            )
                        )
                ann_obj = Annotation(
                    id=ann["id"],
                    type=ann["type"],
                    anchors=anchors,
                    style=ann.get("style", {}),
                    persistent=ann.get("persistent", True),
                )
                win_data.annotations[ann_obj.id] = ann_obj

        return win_data

    def append_bar(self, window_id: str, bar: Bar | dict) -> None:
        win_data = self.get_or_create_window_data(window_id)
        if isinstance(bar, dict):
            bar = Bar(
                t_open=bar["t_open"],
                t_close=bar["t_close"],
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=float(bar.get("volume", 0.0)),
                color_override=bar.get("color_override"),
                fill_override=bar.get("fill_override"),
            )
        win_data.bars.append(validate_bar(bar))

    def update_bar(self, window_id: str, bar: Bar | dict) -> None:
        win_data = self.get_or_create_window_data(window_id)
        if isinstance(bar, dict):
            bar = Bar(
                t_open=bar["t_open"],
                t_close=bar["t_close"],
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                volume=float(bar.get("volume", 0.0)),
                color_override=bar.get("color_override"),
                fill_override=bar.get("fill_override"),
            )
        validated = validate_bar(bar)
        if win_data.bars:
            win_data.bars[-1] = validated
        else:
            win_data.bars.append(validated)

    def set_annotation(self, window_id: str, annotation: Annotation | dict) -> None:
        win_data = self.get_or_create_window_data(window_id)
        if isinstance(annotation, dict):
            raw_anchors = annotation.get("anchors", [])
            anchors = []
            for a in raw_anchors:
                if isinstance(a, Anchor):
                    anchors.append(a)
                elif isinstance(a, dict):
                    anchors.append(
                        Anchor(
                            t=a.get("t"),
                            price=float(a["price"]) if a.get("price") is not None else None,
                            x_px=float(a["x_px"]) if a.get("x_px") is not None else None,
                            y_px=float(a["y_px"]) if a.get("y_px") is not None else None,
                            mode=a.get("mode", "data"),
                        )
                    )
            annotation = Annotation(
                id=annotation["id"],
                type=annotation["type"],
                anchors=anchors,
                style=annotation.get("style", {}),
                persistent=annotation.get("persistent", True),
            )
        win_data.annotations[annotation.id] = annotation

    def remove_annotation(self, window_id: str, annotation_id: str) -> None:
        win_data = self.get_or_create_window_data(window_id)
        win_data.annotations.pop(annotation_id, None)

    def set_topbar_block(self, window_id: str, block: TopBarBlock | dict) -> None:
        win_data = self.get_or_create_window_data(window_id)
        if isinstance(block, dict):
            block = TopBarBlock(
                block_id=block["block_id"],
                position=block["position"],
                content=block["content"],
                ttl_ms=block.get("ttl_ms"),
            )
        win_data.topbar_blocks[block.block_id] = block
