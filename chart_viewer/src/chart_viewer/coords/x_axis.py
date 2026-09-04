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
    right_index: float = 0.0  # Bar index positioned at right margin (before future space)
    config: ViewerConfig = field(default_factory=ViewerConfig.from_env)

    # Debounce tracking for data.request_more
    _last_data_request_time: float = 0.0

    @property
    def future_space_px(self) -> float:
        return self.future_space_bars * self.candle_width_px

    @property
    def chart_width_px(self) -> float:
        return max(1.0, self.viewport_width_px - self.future_space_px)

    @property
    def visible_bars(self) -> int:
        return max(1, math.floor(self.chart_width_px / self.candle_width_px))

    @property
    def min_visible_bar_index(self) -> float:
        """Leftmost visible bar index in viewport."""
        return self.right_index - self.visible_bars

    def bar_to_x(self, bar_index: float) -> float:
        """Convert bar index to pixel X coordinate."""
        # right_index sits at (viewport_width_px - future_space_px)
        anchor_x = self.viewport_width_px - self.future_space_px
        return anchor_x - (self.right_index - bar_index) * self.candle_width_px

    def x_to_bar(self, pixel_x: float) -> float:
        """Convert pixel X coordinate to fractional bar index."""
        anchor_x = self.viewport_width_px - self.future_space_px
        delta_px = anchor_x - pixel_x
        return self.right_index - (delta_px / self.candle_width_px)

    def zoom(self, factor: float, anchor_mouse_x: float | None = None) -> bool:
        """Zoom by factor keeping the bar under anchor_mouse_x stationary.

        factor > 1 zooms in (wider candles), factor < 1 zooms out.
        Returns True if zoom changed, False if clamped.
        """
        old_width = self.candle_width_px
        min_bars = self.config.min_visible_bars

        # Max candle width is limited by min_visible_bars:
        # (min_bars + future_space_bars) * new_width <= viewport_width
        max_allowed_width = self.viewport_width_px / max(1, (min_bars + self.future_space_bars))
        max_allowed_width = min(self.config.max_candle_width_px, max_allowed_width)

        new_width = old_width * factor
        new_width = max(self.config.min_candle_width_px, min(max_allowed_width, new_width))

        # Check hard-limit: minimum visible bars >= min_visible_bars
        if (self.viewport_width_px - self.future_space_bars * new_width) / new_width < min_bars:
            new_width = max_allowed_width

        if abs(new_width - old_width) < 1e-4:
            return False

        if anchor_mouse_x is None:
            anchor_mouse_x = self.viewport_width_px / 2.0

        # Preserve the bar under anchor_mouse_x
        bar_at_anchor = self.x_to_bar(anchor_mouse_x)

        # Apply new width
        self.candle_width_px = new_width

        # Compute new right_index so bar_at_anchor stays at anchor_mouse_x
        new_anchor_x = self.viewport_width_px - self.future_space_px
        self.right_index = bar_at_anchor + (new_anchor_x - anchor_mouse_x) / new_width
        return True

    def pan(self, delta_px: float) -> None:
        """Pan viewport horizontally by pixel delta."""
        delta_bars = delta_px / self.candle_width_px
        self.right_index += delta_bars

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
