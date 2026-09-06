"""Domain entities for Chart Viewer matching spec v2.2."""

from __future__ import annotations
from typing import Literal
import msgspec


class Calendar(msgspec.Struct):
    calendar_type: Literal["continuous", "session"] = "continuous"
    epoch_utc: int = 0
    bar_duration_sec: int = 86400  # Default daily = 86400s
    daily_anchor: str = "00:00 UTC"
    weekly_anchor_weekday: int = 1  # 1 = Monday (Binance conform)
    monthly_anchor_rule: str = "first_of_month_00_utc"
    gap_display_mode: Literal["compact", "gap"] = "compact"


class Instrument(msgspec.Struct):
    symbol: str
    exchange: str
    currency: str
    tick_size: float
    calendar: Calendar


class Timeframe(msgspec.Struct):
    unit: Literal["s", "min", "h", "D", "W", "M"]
    multiplier: int

    def to_seconds(self) -> int:
        """Convert timeframe duration to seconds."""
        multipliers = {
            "s": 1,
            "min": 60,
            "h": 3600,
            "D": 86400,
            "W": 604800,
            "M": 2592000,  # ~30 days
        }
        return self.multiplier * multipliers.get(self.unit, 60)

    def to_string(self) -> str:
        return f"{self.multiplier}{self.unit}"


class Bar(msgspec.Struct):
    t_open: int  # Epoch ms or sec
    t_close: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    color_override: str | None = None
    fill_override: str | None = None
    is_valid: bool = True  # Set to False if high < max(o, c) or low > min(o, c)


class Series(msgspec.Struct):
    series_id: str
    instrument: Instrument
    timeframe: Timeframe
    bars: list[Bar] = []
    style_defaults: dict | None = None


class OverlayPoint(msgspec.Struct):
    t: int
    value: float
    value2: float | None = None  # Second value for bands (e.g. upper/lower band)


class Overlay(msgspec.Struct):
    overlay_id: str
    type: Literal["line", "band", "histogram", "marker"]
    series_id: str
    values: list[OverlayPoint]
    style: dict = {}
    pane: str = "main"                                      # Pane this overlay belongs to
    origin: Literal["bottom", "center"] = "bottom"          # Histogram origin mode


class Anchor(msgspec.Struct):
    t: int | None = None
    price: float | None = None
    x_px: float | None = None
    y_px: float | None = None
    mode: Literal["data", "pixel"] = "data"


class Annotation(msgspec.Struct):
    id: str
    type: Literal["hline", "trendline", "rect", "text", "trade_marker"]
    anchors: list[Anchor]
    style: dict = {}
    persistent: bool = True


class TopBarBlock(msgspec.Struct):
    block_id: str
    position: dict[str, int]  # {"row": int, "col": int}
    content: str
    ttl_ms: int | None = None


class WindowState(msgspec.Struct):
    window_id: str
    window_type: Literal["chart", "watchlist"] = "chart"
    symbol: str | None = None
    timeframe: Timeframe | None = None
    viewport: dict = {}  # candle_width_px, pan_offset, etc.
    y_axis_mode: Literal["auto", "manual", "log", "linear"] = "auto"
    annotations: list[Annotation] = []
    overlays: list[Overlay] = []
    sync_group_id: str | None = None
    color_flag: int = 0
    # watchlist-specific
    list_id: str | None = None
    columns: list[str] | None = None
    sort_column: str | None = None
    sort_ascending: bool = True


class WatchlistRow(msgspec.Struct):
    symbol: str
    cells: dict[str, str | float | int]


class WatchlistState(msgspec.Struct):
    list_id: str
    display_name: str
    columns: list[str]
    rows: list[WatchlistRow]
    sort_column: str | None = None
    sort_ascending: bool = True
    color_flag: int = 0


class MonitorInfo(msgspec.Struct):
    """Information about a connected display monitor."""
    index: int
    name: str = ""
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0
    is_primary: bool = False


class WindowGeometry(msgspec.Struct):
    """Window position and size descriptor saved in setups."""
    window_id: str
    window_type: str  # "chart" or "watchlist"
    position: dict   # {"x": int, "y": int}
    size: dict       # {"width": int, "height": int}
    color_flag: int = 0
    monitor: MonitorInfo | None = None


class WindowSetup(msgspec.Struct, kw_only=True):
    """A named window layout setup persisted to Supabase."""
    setup_name: str
    id: str = ""
    monitor_count: int = 1
    created_at: str = ""
    updated_at: str = ""
    windows: list[WindowGeometry] = []
