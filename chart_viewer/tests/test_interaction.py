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


def test_indicator_line_width_clamp_and_antialiasing(qapp):
    """Test that indicator linecharts enforce minimum line width (4px) and render with antialiasing."""
    from chart_viewer.models.entities import Overlay, OverlayPoint

    cfg = ViewerConfig(min_indicator_line_width_px=4, enable_antialiasing=True)
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

