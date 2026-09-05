"""Deterministic color resolution according to Section 2.2."""

from __future__ import annotations
from dataclasses import dataclass
from chart_viewer.models.entities import Bar
from chart_viewer.config import ViewerConfig


@dataclass(frozen=True)
class ResolvedBarColor:
    border_color: str
    fill_color: str | None  # None indicates hollow
    border_width: int = 1
    is_hollow: bool = False


def resolve_bar_color(
    bar: Bar,
    series_style: dict | None = None,
    config: ViewerConfig = None,
) -> ResolvedBarColor:
    """Resolve candle color following strict 3-tier priority:

    1. Bar.color_override / Bar.fill_override
    2. Series.style_defaults
    3. Global Application Defaults (TC2000 style: thin 1px lines):
       - Up: Blue hollow (1px border)
       - Down: Magenta filled (1px border)
    """
    cfg = config
    is_up = bar.close >= bar.open

    # Tier 3: Global Defaults
    if is_up:
        def_border = cfg.default_up_color
        def_fill = None
        def_hollow = True
    else:
        def_border = cfg.default_down_color
        def_fill = cfg.default_down_color
        def_hollow = False
    def_width = 1

    # Tier 2: Series Style Defaults
    series_style = series_style or {}
    if is_up:
        style_border = series_style.get("up_color", def_border)
        # If up_fill is specified as None or 'transparent'/'hollow', make it hollow
        style_fill = series_style.get("up_fill", def_fill)
        style_hollow = series_style.get("up_hollow", def_hollow)
    else:
        style_border = series_style.get("down_color", def_border)
        style_fill = series_style.get("down_fill", def_fill)
        style_hollow = series_style.get("down_hollow", def_hollow)
    style_width = series_style.get("border_width", def_width)

    # Tier 1: Bar-level Overrides
    final_border = bar.color_override if bar.color_override is not None else style_border

    if bar.fill_override is not None:
        if bar.fill_override in ("hollow", "transparent", "none", ""):
            final_fill = None
            final_hollow = True
        else:
            final_fill = bar.fill_override
            final_hollow = False
    else:
        final_fill = style_fill
        final_hollow = style_hollow

    return ResolvedBarColor(
        border_color=final_border,
        fill_color=final_fill,
        border_width=style_width,
        is_hollow=final_hollow,
    )
