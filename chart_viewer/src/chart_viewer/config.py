"""Central configuration module for the Chart Viewer.

All timers, intervals, limits, and magic numbers are strictly configurable
here and can be overridden via environment variables.
"""

from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass
class ViewerConfig:
    # Rendering & Refresh limits
    render_fps: int = 60  # Range 30 - 120 Hz
    max_fps_interval_ms: int = 16  # Derived: 1000 // render_fps

    # Network & Transport
    protocol_version: str = "1.0"
    reconnect_backoff_initial_ms: int = 100
    reconnect_backoff_max_ms: int = 5000
    reconnect_backoff_factor: float = 2.0
    resync_rate_limit_sec: float = 5.0  # Max 1 resync per 5s per window

    # Data Boundaries & Debounce
    data_request_debounce_ms: int = 250
    min_visible_bars: int = 6  # Hard-limit zoom
    future_space_bars: int = 15  # Default future space on right side
    thin_bar_threshold_px: float = 3.0  # candle_width_px < threshold -> Thin-Bar Mode

    # Interaction & Hit Testing
    hit_test_radius_px: float = 6.0  # Handle & line detection radius

    # Axes & Padding
    margin_top_pct: float = 0.05  # 5% margin top
    margin_bottom_pct: float = 0.05  # 5% margin bottom
    default_candle_width_px: float = 8.0
    min_candle_width_px: float = 1.0
    max_candle_width_px: float = 60.0
    configured_wick_px: int = 1

    # Default styling (Application Defaults: 3rd priority tier)
    # Up: Blue filled (#2962FF), Down: Violet hollow (#7B1FA2), 4px border (or 1-4px)
    default_up_color: str = "#2962FF"
    default_down_color: str = "#7B1FA2"
    default_background_color: str = "#131722"
    default_grid_color: str = "#2A2E39"
    default_text_color: str = "#D1D4DC"
    default_crosshair_color: str = "#758696"

    # Screenshot settings (for agent UI debugging)
    screenshot_width: int = 640
    screenshot_height: int = 480
    screenshot_hires_width: int = 800
    screenshot_hires_height: int = 600
    screenshot_timeout_sec: float = 10.0
    screenshot_output_dir: str = "/home/daniel/QJM/dsh_playground"
    screenshot_mode: str = "letterbox"  # 'letterbox', 'fit', or 'stretch'
    screenshot_sharpen_amount: float = 0.5  # Edge enhancement to eliminate downscaling blur

    @classmethod
    def from_env(cls) -> ViewerConfig:
        """Create config populated from environment variables with fallbacks."""
        fps = int(os.getenv("CV_RENDER_FPS", "60"))
        fps = max(30, min(120, fps))  # Clamp between 30 and 120 Hz
        return cls(
            render_fps=fps,
            max_fps_interval_ms=1000 // fps,
            protocol_version=os.getenv("CV_PROTOCOL_VERSION", "1.0"),
            reconnect_backoff_initial_ms=int(os.getenv("CV_RECONNECT_BACKOFF_INITIAL_MS", "100")),
            reconnect_backoff_max_ms=int(os.getenv("CV_RECONNECT_BACKOFF_MAX_MS", "5000")),
            resync_rate_limit_sec=float(os.getenv("CV_RESYNC_RATE_LIMIT_SEC", "5.0")),
            data_request_debounce_ms=int(os.getenv("CV_DATA_REQUEST_DEBOUNCE_MS", "250")),
            min_visible_bars=int(os.getenv("CV_MIN_VISIBLE_BARS", "6")),
            future_space_bars=int(os.getenv("CV_FUTURE_SPACE_BARS", "15")),
            thin_bar_threshold_px=float(os.getenv("CV_THIN_BAR_THRESHOLD_PX", "3.0")),
            hit_test_radius_px=float(os.getenv("CV_HIT_TEST_RADIUS_PX", "6.0")),
            margin_top_pct=float(os.getenv("CV_MARGIN_TOP_PCT", "0.05")),
            margin_bottom_pct=float(os.getenv("CV_MARGIN_BOTTOM_PCT", "0.05")),
            screenshot_width=int(os.getenv("CV_SCREENSHOT_WIDTH", "640")),
            screenshot_height=int(os.getenv("CV_SCREENSHOT_HEIGHT", "480")),
            screenshot_hires_width=int(os.getenv("CV_SCREENSHOT_HIRES_WIDTH", "800")),
            screenshot_hires_height=int(os.getenv("CV_SCREENSHOT_HIRES_HEIGHT", "600")),
            screenshot_timeout_sec=float(os.getenv("CV_SCREENSHOT_TIMEOUT_SEC", "10.0")),
            screenshot_output_dir=os.getenv("CV_SCREENSHOT_DIR", "/home/daniel/QJM/dsh_playground"),
            screenshot_mode=os.getenv("CV_SCREENSHOT_MODE", "letterbox"),
            screenshot_sharpen_amount=float(os.getenv("CV_SCREENSHOT_SHARPEN_AMOUNT", "0.5")),
        )



GLOBAL_CONFIG = ViewerConfig.from_env()

