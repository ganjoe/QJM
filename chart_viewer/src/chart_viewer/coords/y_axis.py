"""Y-Axis coordinate transformation according to Section 5.2."""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Literal
from chart_viewer.config import GLOBAL_CONFIG, ViewerConfig


@dataclass
class YAxisTransform:
    viewport_height_px: float = 600.0
    mode: Literal["linear", "log"] = "linear"
    margin_top_pct: float = 0.05
    margin_bottom_pct: float = 0.05
    p_min: float = 1.0
    p_max: float = 100.0
    is_mode_forced: bool = False
    config: ViewerConfig = field(default_factory=ViewerConfig.from_env)

    # Effective computed bounds including margins
    p_top: float = 105.0
    p_bottom: float = 0.95
    l_top: float = 0.0
    l_bottom: float = 0.0

    def fit_range(
        self,
        min_val: float,
        max_val: float,
        requested_mode: Literal["linear", "log"] = "linear",
    ) -> bool:
        """Fit Y-range to min_val and max_val with margin padding.

        Returns True if mode was forced from log to linear due to non-positive values.
        """
        # Guard: If log requested but values <= 0
        forced = False
        if requested_mode == "log":
            if min_val <= 0 or max_val <= 0:
                self.mode = "linear"
                self.is_mode_forced = True
                forced = True
            else:
                self.mode = "log"
                self.is_mode_forced = False
        else:
            self.mode = "linear"
            self.is_mode_forced = False

        if max_val <= min_val:
            # Avoid division by zero
            max_val = min_val + 1.0

        self.p_min = min_val
        self.p_max = max_val

        if self.mode == "linear":
            r = max_val - min_val
            self.p_top = max_val + r * self.margin_top_pct
            self.p_bottom = min_val - r * self.margin_bottom_pct
            if self.p_top == self.p_bottom:
                self.p_top += 1.0
                self.p_bottom -= 1.0
        else:
            l_max = math.log(max_val)
            l_min = math.log(min_val)
            l_r = l_max - l_min
            self.l_top = l_max + l_r * self.margin_top_pct
            self.l_bottom = l_min - l_r * self.margin_bottom_pct
            self.p_top = math.exp(self.l_top)
            self.p_bottom = math.exp(self.l_bottom)

        return forced

    def price_to_y(self, price: float) -> float:
        """Convert price to pixel Y coordinate (0 = top, height = bottom)."""
        h = max(1.0, self.viewport_height_px)
        if self.mode == "linear":
            span = self.p_top - self.p_bottom
            if span <= 0:
                return h / 2.0
            norm = (price - self.p_bottom) / span
            return h * (1.0 - norm)
        else:
            if price <= 0:
                price = 1e-6
            l_price = math.log(price)
            span = self.l_top - self.l_bottom
            if span <= 0:
                return h / 2.0
            norm = (l_price - self.l_bottom) / span
            return h * (1.0 - norm)

    def y_to_price(self, pixel_y: float) -> float:
        """Convert pixel Y coordinate to price."""
        h = max(1.0, self.viewport_height_px)
        norm = 1.0 - (pixel_y / h)
        if self.mode == "linear":
            return self.p_bottom + norm * (self.p_top - self.p_bottom)
        else:
            l_price = self.l_bottom + norm * (self.l_top - self.l_bottom)
            return math.exp(l_price)

    def manual_scale(self, factor: float) -> None:
        """Manually scale Y-axis range (manual zoom)."""
        mid = (self.p_top + self.p_bottom) / 2.0
        half_span = (self.p_top - self.p_bottom) / 2.0 * factor
        self.p_top = mid + half_span
        self.p_bottom = mid - half_span
