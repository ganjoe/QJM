"""X-Axis coordinate transformation according to Section 5.1."""

from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig


@dataclass
class XAxisTransform:
    viewport_width_px: float = 800.0
    candle_width_px: float = 8.0
    future_space_bars: int = 15
    right_margin_pct: float = field(default=None)  # 10% margin distance from right edge
    touch_left_border: bool = field(default=None)   # Rule: Chart touches left window edge (x=0)
    pin_to_right: bool = True       # When True, zooming keeps the chart tacked to the 10% right margin
    right_index: float = 0.0        # Bar index positioned at right margin (before future space)
    config: ViewerConfig = field(default_factory=ViewerConfig)

    # Debounce tracking for data.request_more
    _last_data_request_time: float = 0.0

    def __post_init__(self):
        cfg = self.config
        if self.right_margin_pct is None:
            self.right_margin_pct = getattr(cfg, "right_margin_pct", 0.10)
        if self.touch_left_border is None:
            self.touch_left_border = getattr(cfg, "touch_left_border", True)

    @property
    def future_space_px(self) -> float:
        if self.right_margin_pct > 0:
            return self.viewport_width_px * self.right_margin_pct
        return self.future_space_bars * self.candle_width_px

    @property
    def anchor_x(self) -> float:
        """Right anchor pixel where right_index bar sits (before future margin)."""
        return self.viewport_width_px - self.future_space_px

    @property
    def chart_width_px(self) -> float:
        return max(1.0, self.viewport_width_px - self.future_space_px)

    @property
    def visible_bars(self) -> int:
        return max(1, math.floor(self.chart_width_px / self.candle_width_px))

    @property
    def min_visible_bar_index(self) -> float:
        """Leftmost visible bar index in viewport (at pixel x=0)."""
        return self.x_to_bar(0.0)

    def bar_to_x(self, bar_index: float) -> float:
        """Convert bar index to pixel X coordinate."""
        return self.anchor_x - (self.right_index - bar_index) * self.candle_width_px

    def x_to_bar(self, pixel_x: float) -> float:
        """Convert pixel X coordinate to fractional bar index."""
        delta_px = self.anchor_x - pixel_x
        return self.right_index - (delta_px / self.candle_width_px)

    def ensure_touches_left(self) -> bool:
        """Enforce Rule: Chart touches the left window edge (x=0).

        Adjusts candle_width_px if bar 0 is currently detached from the left border (x > 0).
        Returns True if width changed, False otherwise.
        """
        if not self.touch_left_border or self.right_index <= 0:
            return False
        if self.pin_to_right:
            min_width = self.anchor_x / max(1.0, self.right_index)
            if self.candle_width_px < min_width:
                self.candle_width_px = min_width
                return True
        else:
            if self.candle_width_px > 0:
                min_right_idx = self.anchor_x / self.candle_width_px
                if self.right_index < min_right_idx:
                    self.right_index = min_right_idx
                    return True
        return False

    def set_viewport_width(self, new_width_px: float) -> None:
        """Update viewport width, scaling candle width proportionally on width changes.

        Widening window widens candles; narrowing window compresses candles (stauchen),
        ensuring visible bar range is preserved without obscuring candles.
        """
        if new_width_px <= 10.0 or abs(new_width_px - self.viewport_width_px) < 1.0:
            return

        old_anchor = self.anchor_x
        self.viewport_width_px = new_width_px
        new_anchor = self.anchor_x

        if old_anchor > 10.0 and new_anchor > 10.0:
            ratio = new_anchor / old_anchor
            self.candle_width_px = max(self.config.min_candle_width_px, self.candle_width_px * ratio)

        self.ensure_touches_left()

    def zoom(
        self,
        factor: float,
        anchor_mouse_x: float | None = None,
        pin_to_right: bool | None = None,
    ) -> bool:
        """Zoom by factor keeping the bar under anchor stationary or tacked to right margin.

        factor > 1 zooms in (wider candles), factor < 1 zooms out.
        Returns True if zoom changed, False if clamped.
        """
        old_width = self.candle_width_px
        min_bars = self.config.min_visible_bars

        # Max candle width is limited by min_visible_bars:
        avail_w = self.viewport_width_px - self.future_space_px
        max_allowed_width = avail_w / max(1, min_bars)
        max_allowed_width = min(self.config.max_candle_width_px, max_allowed_width)

        new_width = old_width * factor
        new_width = max(self.config.min_candle_width_px, min(max_allowed_width, new_width))

        # Check hard-limit: minimum visible bars >= min_visible_bars
        if (self.viewport_width_px - self.future_space_px) / new_width < min_bars:
            new_width = max_allowed_width

        if abs(new_width - old_width) < 1e-4:
            return False

        if pin_to_right is None:
            pin_to_right = self.pin_to_right and (anchor_mouse_x is None)

        if pin_to_right:
            if hasattr(self, "latest_bar_index") and self.latest_bar_index >= 0:
                self.right_index = self.latest_bar_index
                self.pin_to_right = True

            # Keep the latest bar pinned ("angetackert") at the 10% right margin
            self.candle_width_px = new_width
            self.ensure_touches_left()
            return True

        if anchor_mouse_x is None:
            anchor_mouse_x = self.viewport_width_px / 2.0

        # Preserve the bar under anchor_mouse_x
        bar_at_anchor = self.x_to_bar(anchor_mouse_x)

        # Compute new right_index so bar_at_anchor stays at anchor_mouse_x
        new_anchor_x = self.anchor_x
        new_right_index = bar_at_anchor + (new_anchor_x - anchor_mouse_x) / new_width

        # Clamp against pulling into future space (latest bar cannot be pulled into screen interior leaving blank space on the right)
        if hasattr(self, "latest_bar_index") and self.latest_bar_index >= 0:
            if new_right_index >= self.latest_bar_index:
                new_right_index = self.latest_bar_index
                self.pin_to_right = True

        # Only clamp if bar 0 was touching/crossing the left border (<= 0) and zoom would detach it (> 0)
        old_bar0_x = self.anchor_x - self.right_index * old_width
        new_bar0_x = new_anchor_x - new_right_index * new_width
        if self.touch_left_border and new_width > 0 and old_bar0_x <= 0.0 and new_bar0_x > 0.0:
            new_right_index = new_anchor_x / new_width

        self.candle_width_px = new_width
        self.right_index = new_right_index
        return True

    def pan(self, delta_px: float) -> None:
        """Pan viewport horizontally by pixel delta.

        Dragging right (delta_px > 0) pulls the chart right (revealing older historical bars).
        Dragging left (delta_px < 0) pulls the chart left (revealing newer bars / towards latest bar).
        """
        delta_bars = delta_px / self.candle_width_px
        self.right_index -= delta_bars

        # Cannot pan past the latest bar into empty future margin (when dragging left towards today)
        if hasattr(self, "latest_bar_index") and self.latest_bar_index >= 0:
            if self.right_index >= self.latest_bar_index:
                self.right_index = self.latest_bar_index
                self.pin_to_right = True

        # Rule: Chart touches the left border (cannot pan past bar 0 into empty past space)
        if self.touch_left_border and self.candle_width_px > 0:
            min_right_idx = self.anchor_x / self.candle_width_px
            if self.right_index < min_right_idx:
                self.right_index = min_right_idx

    def should_request_more_data(self, earliest_loaded_bar_index: float) -> bool:
        """Check if viewport has reached the historical edge, with debouncing."""
        # Trigger if visible left edge is within 10 bars of loaded history
        if self.min_visible_bar_index <= earliest_loaded_bar_index + 10:
            now = time.time()
            debounce_sec = self.config.data_request_debounce_ms / 1000.0
            if now - self._last_data_request_time >= debounce_sec:
                self._last_data_request_time = now
                return True
        return False
