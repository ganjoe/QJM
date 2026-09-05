"""Y-Axis coordinate transformation according to Section 5.2."""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Literal
from chart_viewer.config import ViewerConfig


@dataclass
class YAxisTransform:
    viewport_height_px: float = 600.0
    mode: Literal["linear", "log"] = "linear"
    margin_top_pct: float = field(default=None)
    margin_bottom_pct: float = field(default=None)
    min_padding_px: float = field(default=None)
    top_margin_ratio: float = field(default=None)  # TC2000 headroom ceiling ratio
    p_min: float = 1.0
    p_max: float = 100.0
    is_mode_forced: bool = False
    config: ViewerConfig = field(default_factory=ViewerConfig)

    # Effective computed bounds including margins
    p_top: float = 115.0
    p_bottom: float = 0.92
    l_top: float = 0.0
    l_bottom: float = 0.0

    def __post_init__(self):
        cfg = self.config
        if self.margin_top_pct is None:
            self.margin_top_pct = getattr(cfg, "margin_top_pct", 0.15)
        if self.margin_bottom_pct is None:
            self.margin_bottom_pct = getattr(cfg, "margin_bottom_pct", 0.08)
        if self.min_padding_px is None:
            self.min_padding_px = getattr(cfg, "min_padding_px", 25.0)
        if self.top_margin_ratio is None:
            self.top_margin_ratio = self.margin_top_pct

    def fit_range(
        self,
        min_val: float,
        max_val: float,
        requested_mode: Literal["linear", "log"] = "linear",
    ) -> bool:
        """Fit Y-range to min_val and max_val with guaranteed margin padding.

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

        h = max(1.0, self.viewport_height_px)
        effective_top_margin = max(self.margin_top_pct, self.top_margin_ratio)

        if self.mode == "linear":
            r = max_val - min_val
            # Convert min_padding_px into price space to ensure elements never touch borders
            min_pad_price = (r / max(1.0, h - 2 * self.min_padding_px)) * self.min_padding_px
            top_padding = max(r * effective_top_margin, min_pad_price)
            bottom_padding = max(r * self.margin_bottom_pct, min_pad_price)

            self.p_top = max_val + top_padding
            self.p_bottom = min_val - bottom_padding
            if self.p_top == self.p_bottom:
                self.p_top += 1.0
                self.p_bottom -= 1.0
        else:
            l_max = math.log(max_val)
            l_min = math.log(min_val)
            l_r = l_max - l_min
            min_pad_log = (l_r / max(1.0, h - 2 * self.min_padding_px)) * self.min_padding_px
            top_padding = max(l_r * effective_top_margin, min_pad_log)
            bottom_padding = max(l_r * self.margin_bottom_pct, min_pad_log)

            self.l_top = l_max + top_padding
            self.l_bottom = l_min - bottom_padding
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
        """Manually scale Y-axis range anchored at the bottom of the window.

        factor > 1 zooms out (compresses chart downward, more headroom).
        factor < 1 zooms in (expands chart upward, less headroom).
        Guarantees p_max never moves closer to the top border than min_padding_px.
        """
        h = max(1.0, self.viewport_height_px)
        current_y = self.price_to_y(self.p_max)
        if current_y <= 0:
            effective_margin = max(self.margin_top_pct, self.top_margin_ratio or self.margin_top_pct)
            current_y = max(self.min_padding_px, h * effective_margin)

        target_y = current_y * factor
        self.set_top_boundary_px(target_y)

    def set_top_boundary_px(self, target_y_px: float) -> None:
        """Set the top boundary for p_max in pixel coordinates (anchored at bottom)."""
        h = max(1.0, self.viewport_height_px)
        target_y = max(self.min_padding_px, min(h - self.min_padding_px - 30.0, target_y_px))
        denom = 1.0 - (target_y / h)
        if denom > 1e-4:
            if self.mode == "linear":
                diff = max(1e-4, self.p_max - self.p_bottom)
                self.p_top = self.p_bottom + (diff / denom)
                self.top_margin_ratio = target_y / h
            else:
                l_diff = max(1e-4, math.log(max(1e-6, self.p_max)) - self.l_bottom)
                self.l_top = self.l_bottom + (l_diff / denom)
                self.p_top = math.exp(self.l_top)
                self.top_margin_ratio = target_y / h

    def reset_boundary(self) -> None:
        """Reset top boundary to config default."""
        self.top_margin_ratio = self.margin_top_pct
        self.fit_range(self.p_min, self.p_max, requested_mode=self.mode)


