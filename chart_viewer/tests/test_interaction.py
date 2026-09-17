from chart_viewer.config import ViewerConfig
"""Tests for interaction model and ephemeral measure tool (Section 7 & Criterion 3)."""

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QMouseEvent
from chart_viewer.ui.canvas import ChartCanvas
from chart_viewer.core.state_manager import WindowData
from chart_viewer.models.entities import Bar, Annotation, Anchor



def test_canvas_measure_tool_lifecycle(qapp):
    """Test pane mouse interaction for measure tool lifecycle."""
    canvas = ChartCanvas(window_id="test-win", config=ViewerConfig())
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000, t_close=2000, open=100.0, high=110.0, low=90.0, close=105.0),
        Bar(t_open=2000, t_close=3000, open=105.0, high=115.0, low=95.0, close=110.0),
    ]
    canvas.set_window_data(win_data)
    main_pane = canvas._panes.get("main")
    assert main_pane is not None
    main_pane.resize(800, 500)

    # 1. Simulate mouse press on empty chart area (e.g. at 200, 300)
    press_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(200, 300),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_pane.mousePressEvent(press_event)

    assert main_pane._is_measuring is True
    assert main_pane._measure_start_pos == QPointF(200, 300)

    # 2. Simulate mouse move (measuring active)
    move_event = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(350, 200),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_pane.mouseMoveEvent(move_event)
    assert main_pane._is_measuring is True

    # 3. Simulate mouse release -> MUST immediately discard measurement and never persist
    release_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(350, 200),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_pane.mouseReleaseEvent(release_event)

    assert main_pane._is_measuring is False
    assert main_pane._measure_start_pos is None
    # Verify no persistent annotations were added
    assert len(win_data.annotations) == 0


def test_ctrl_wheel_horizontal_scroll(qapp):
    """Test horizontal scrolling with mouse wheel when Ctrl modifier is held."""
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtCore import QPoint

    cfg = ViewerConfig(wheel_scroll_step_bars=5.0)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    # 100 bars so we have room to scroll left and right
    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(100)
    ]
    canvas.set_window_data(win_data)

    initial_right_idx = canvas.x_trans.right_index
    initial_candle_w = canvas.x_trans.candle_width_px
    assert initial_right_idx == 99.0

    # 1. Wheel UP with Ctrl held (angleDelta = +120) -> should scroll backward in time (left) by 5 bars
    wheel_up = QWheelEvent(
        QPointF(200, 300),
        QPointF(200, 300),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    canvas.wheelEvent(wheel_up)

    # Right index should have moved left by 5 bars (99.0 - 5.0 = 94.0)
    assert canvas.x_trans.right_index == 94.0
    # Candle width (zoom factor) MUST remain unchanged!
    assert canvas.x_trans.candle_width_px == initial_candle_w
    assert canvas.x_trans.pin_to_right is False

    # 2. Wheel DOWN with Ctrl held (angleDelta = -120) -> should scroll forward in time (right) by 5 bars
    wheel_down = QWheelEvent(
        QPointF(200, 300),
        QPointF(200, 300),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    canvas.wheelEvent(wheel_down)

    # Right index should have moved right back to 99.0 (pinned to latest)
    assert canvas.x_trans.right_index == 99.0
    assert canvas.x_trans.candle_width_px == initial_candle_w


def test_indicator_line_width_and_antialiasing(qapp):
    """Indicator lines render with antialiasing and no minimum-width floor."""
    from chart_viewer.models.entities import Overlay, OverlayPoint

    cfg = ViewerConfig(enable_antialiasing=True)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(20)
    ]
    # Add an indicator overlay with thin width (width: 1)
    overlay_thin = Overlay(
        overlay_id="thin_line",
        series_id="test_series",
        pane="main",
        type="line",
        values=[OverlayPoint(t=1000 + i * 100, value=100.0 + i) for i in range(20)],
        style={"color": "#00E676", "width": 1},
    )
    win_data.overlays["thin_line"] = overlay_thin
    canvas.set_window_data(win_data)

    main_pane = canvas._panes.get("main")
    assert main_pane is not None

    canvas.show()
    qapp.processEvents()
    main_pane.render(main_pane)
    assert main_pane._pixmap is not None


def test_line_width_is_the_explicit_pixel_count():
    """style['width'] IS the pixel width — never rounded up to a floor."""
    from chart_viewer.ui.pane import resolve_line_width

    # No explicit width -> configured default (1px out of the box)
    assert resolve_line_width({}) == 1
    assert resolve_line_width({"color": "#FFFFFF"}) == 1
    assert resolve_line_width(None) == 1

    # Explicit widths are used verbatim, including values below the old 4px floor
    assert resolve_line_width({"width": 1}) == 1
    assert resolve_line_width({"width": 2}) == 2
    assert resolve_line_width({"width": 3}) == 3
    assert resolve_line_width({"width": 5}) == 5

    # Explicit width always beats the default
    assert resolve_line_width({"width": 2}, default_width=4) == 2
    # ...and the default is only used when nothing is set
    assert resolve_line_width({}, default_width=3) == 3

    # A width is never reduced to 0 (that would be Qt's cosmetic hairline)
    assert resolve_line_width({"width": 0}) == 1
    assert resolve_line_width({"width": -2}) == 1


def test_rendered_thickness_scales_with_width(qapp):
    """A wider style actually paints a thicker line (pen width reaches the painter)."""
    from PySide6.QtGui import QColor
    from chart_viewer.models.entities import Overlay, OverlayPoint

    flat_color = "#FF00FF"

    def painted_pixels(width: int) -> int:
        cfg = ViewerConfig(enable_antialiasing=False)
        canvas = ChartCanvas(window_id="w", config=cfg)
        canvas.resize(400, 300)

        win_data = WindowData("w")
        win_data.bars = [
            Bar(t_open=1000 + i * 100, t_close=1100 + i * 100,
                open=100.0, high=101.0, low=99.0, close=100.5)
            for i in range(20)
        ]
        win_data.overlays["flat"] = Overlay(
            overlay_id="flat",
            series_id="s",
            pane="main",
            type="line",
            values=[OverlayPoint(t=1000 + i * 100, value=100.0) for i in range(20)],
            style={"color": flat_color, "width": width},
        )
        canvas.set_window_data(win_data)
        canvas.show()
        qapp.processEvents()

        pane = canvas._panes["main"]
        pane.render(pane)
        img = pane._pixmap.toImage()
        target = QColor(flat_color)
        hits = 0
        for y in range(img.height()):
            for x in range(img.width()):
                px = img.pixelColor(x, y)
                if (abs(px.red() - target.red()) <= 20
                        and abs(px.green() - target.green()) <= 20
                        and abs(px.blue() - target.blue()) <= 20):
                    hits += 1
        return hits

    thin = painted_pixels(1)
    thick = painted_pixels(4)
    assert thin > 0, "1px line was not painted at all"
    assert thick > thin, f"width 4 ({thick}px) should paint more than width 1 ({thin}px)"


def test_thin_indicator_line_is_crisp_one_pixel(qapp):
    """A 1px diagonal indicator line must paint exactly 1px, like the candle borders.

    With antialiasing it was spread over ~1.86 rows per column (measured), which
    made indicator lines look about twice as heavy as the crisp candle strokes.
    """
    from chart_viewer.models.entities import Overlay, OverlayPoint

    def render(crisp: bool, with_line: bool):
        cfg = ViewerConfig(crisp_thin_indicator_lines=crisp)
        canvas = ChartCanvas(window_id="w", config=cfg)
        canvas.resize(400, 300)

        n = 40
        wd = WindowData("w")
        wd.bars = [
            Bar(t_open=1000 + i * 100, t_close=1100 + i * 100,
                open=100.0, high=101.0, low=99.0, close=100.5)
            for i in range(n)
        ]
        if with_line:
            wd.overlays["diag"] = Overlay(
                overlay_id="diag", series_id="s", pane="main", type="line",
                values=[OverlayPoint(t=1000 + i * 100, value=99.2 + i * 0.04) for i in range(n)],
                style={"color": "#FF00FF", "width": 1},
            )
        canvas.set_window_data(wd)
        canvas.show()
        qapp.processEvents()
        img = canvas.grab().toImage()
        canvas.hide()
        qapp.processEvents()
        return img

    def mean_thickness(crisp: bool) -> float:
        with_line = render(crisp, True)
        without = render(crisp, False)
        per_col = []
        for x in range(with_line.width()):
            rows = 0
            for y in range(with_line.height()):
                pa, pb = with_line.pixelColor(x, y), without.pixelColor(x, y)
                if (abs(pa.red() - pb.red()) + abs(pa.green() - pb.green())
                        + abs(pa.blue() - pb.blue())) > 30:
                    rows += 1
            if rows:
                per_col.append(rows)
        assert per_col, "diagonal line was not painted at all"
        return sum(per_col) / len(per_col)

    crisp = mean_thickness(True)
    smooth = mean_thickness(False)

    assert crisp == 1.0, f"crisp 1px line should be exactly 1px, got {crisp:.2f}"
    assert smooth > 1.5, f"antialiased line should be measurably thicker, got {smooth:.2f}"

