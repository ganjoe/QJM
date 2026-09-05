"""Tests for deterministic 3-tier color resolution (Section 2.2)."""

from chart_viewer.models.entities import Bar
from chart_viewer.models.color import resolve_bar_color
from chart_viewer.config import ViewerConfig


def test_tier3_global_defaults():
    cfg = ViewerConfig(default_up_color="#3877FF", default_down_color="#E040FB")

    # Up candle (close >= open): Hollow, 1px border
    up_bar = Bar(t_open=1, t_close=2, open=100.0, high=105.0, low=95.0, close=102.0)
    up_color = resolve_bar_color(up_bar, config=cfg)
    assert up_color.border_color == "#3877FF"
    assert up_color.fill_color is None  # Hollow
    assert up_color.is_hollow is True
    assert up_color.border_width == 1

    # Down candle (close < open): Filled, 1px border
    down_bar = Bar(t_open=1, t_close=2, open=100.0, high=105.0, low=95.0, close=98.0)
    down_color = resolve_bar_color(down_bar, config=cfg)
    assert down_color.border_color == "#E040FB"
    assert down_color.fill_color == "#E040FB"  # Filled
    assert down_color.is_hollow is False
    assert down_color.border_width == 1


def test_tier2_series_style():
    series_style = {
        "up_color": "#00E676",
        "up_fill": "#00E676",
        "down_color": "#FF5252",
        "down_fill": "#FF5252",
        "down_hollow": False,
        "border_width": 2,
    }
    up_bar = Bar(t_open=1, t_close=2, open=100.0, high=105.0, low=95.0, close=102.0)
    up_color = resolve_bar_color(up_bar, series_style=series_style)
    assert up_color.border_color == "#00E676"
    assert up_color.fill_color == "#00E676"
    assert up_color.border_width == 2

    down_bar = Bar(t_open=1, t_close=2, open=100.0, high=105.0, low=95.0, close=98.0)
    down_color = resolve_bar_color(down_bar, series_style=series_style)
    assert down_color.border_color == "#FF5252"
    assert down_color.fill_color == "#FF5252"
    assert down_color.border_width == 2


def test_tier1_bar_override():
    series_style = {"up_color": "#00E676", "border_width": 2}
    bar = Bar(
        t_open=1,
        t_close=2,
        open=100.0,
        high=105.0,
        low=95.0,
        close=102.0,
        color_override="#FFFF00",  # Yellow override
        fill_override="hollow",
    )
    res = resolve_bar_color(bar, config=ViewerConfig(), series_style=series_style)
    assert res.border_color == "#FFFF00"
    assert res.fill_color is None
    assert res.is_hollow is True
