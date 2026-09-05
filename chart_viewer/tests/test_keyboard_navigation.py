"""Tests for keyboard navigation (Arrow Left/Right, single tap, hold >500ms smooth scroll, snap-to-bar)."""

import time
import math
import pytest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent, QFocusEvent

from chart_viewer.config import ViewerConfig
from chart_viewer.coords.x_axis import XAxisTransform
from chart_viewer.ui.canvas import ChartCanvas
from chart_viewer.ui.window import ChartWindow
from chart_viewer.core.state_manager import WindowData
from chart_viewer.models.entities import Bar


def test_x_axis_pan_bars_and_snap():
    """Test XAxisTransform pan_bars and snap_to_bar functionality."""
    cfg = ViewerConfig(touch_left_border=True)
    x_trans = XAxisTransform(
        viewport_width_px=800.0,
        candle_width_px=8.0,
        right_index=100.0,
        config=cfg,
    )
    # 720px / 8px = 90 bars needed to touch left border. With right_index=100, bar 0 is at -80px (off-screen)
    x_trans.latest_bar_index = 150.0
    x_trans.pin_to_right = False

    # Pan left (backward into history)
    changed = x_trans.pan_bars(-1.0)
    assert changed is True
    assert math.isclose(x_trans.right_index, 99.0, abs_tol=1e-4)
    assert x_trans.pin_to_right is False

    # Pan right (forward towards latest bar)
    changed = x_trans.pan_bars(2.5)
    assert changed is True
    assert math.isclose(x_trans.right_index, 101.5, abs_tol=1e-4)

    # Snap to bar
    snapped = x_trans.snap_to_bar()
    assert snapped is True
    assert math.isclose(x_trans.right_index, 102.0, abs_tol=1e-4)

    # Pan past latest bar -> clamps to latest_bar_index and re-pins
    x_trans.pan_bars(100.0)
    assert math.isclose(x_trans.right_index, 150.0, abs_tol=1e-4)
    assert x_trans.pin_to_right is True

    # Pan far left past bar 0 -> clamps to min_right_idx (90.0)
    x_trans.pan_bars(-200.0)
    assert math.isclose(x_trans.right_index, 90.0, abs_tol=1e-4)


def test_canvas_single_tap_left_right(qapp):
    """Test single key press shifts exactly 1 bar immediately."""
    cfg = ViewerConfig(key_scroll_step_bars=1.0, key_scroll_hold_delay_ms=500)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    # 500 bars so history is plentiful
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    initial_right = canvas.x_trans.right_index
    assert initial_right == 499.0

    # 1. Single tap: Arrow Left
    press_left = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press_left)

    # Must immediately move 1 bar left (into history)
    assert math.isclose(canvas.x_trans.right_index, initial_right - 1.0, abs_tol=1e-4)
    assert canvas.x_trans.pin_to_right is False
    assert canvas._key_hold_timer.isActive()

    # Release Left before 500ms
    release_left = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    canvas.keyReleaseEvent(release_left)

    assert not canvas._key_hold_timer.isActive()
    assert not canvas._key_scroll_timer.isActive()
    assert canvas._active_scroll_key is None

    # 2. Single tap: Arrow Right
    press_right = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press_right)

    # Must immediately move 1 bar right (towards latest)
    assert math.isclose(canvas.x_trans.right_index, initial_right, abs_tol=1e-4)
    assert canvas.x_trans.pin_to_right is True

    release_right = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    canvas.keyReleaseEvent(release_right)


def test_canvas_auto_repeat_ignored(qapp):
    """Test OS auto-repeat events do not cause duplicate discrete step shifts."""
    cfg = ViewerConfig(key_scroll_step_bars=1.0)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    initial_right = canvas.x_trans.right_index

    # First press (not auto-repeat)
    press1 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press1)
    assert math.isclose(canvas.x_trans.right_index, initial_right - 1.0, abs_tol=1e-4)

    # Second press from OS auto-repeat
    press_repeat = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Left,
        Qt.KeyboardModifier.NoModifier,
        "",
        True,
    )
    canvas.keyPressEvent(press_repeat)

    # Must stay unchanged (still -1.0, not -2.0)
    assert math.isclose(canvas.x_trans.right_index, initial_right - 1.0, abs_tol=1e-4)

    canvas._stop_key_scrolling()


def test_canvas_hold_smooth_scrolling_and_snap(qapp):
    """Test key hold > 500ms enters smooth scrolling, and key release snaps to bar."""
    cfg = ViewerConfig(
        key_scroll_step_bars=1.0,
        key_scroll_hold_delay_ms=500,
        key_scroll_speed_bars_per_sec=10.0,
    )
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    # Press Arrow Left
    press_left = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press_left)

    # Simulating hold timeout > 500ms
    assert canvas._key_hold_timer.isActive()
    canvas._on_key_hold_timeout()

    assert canvas._is_smooth_scrolling is True
    assert canvas._key_scroll_timer.isActive()

    # Simulate a smooth scroll tick with dt = 0.05s (50ms)
    canvas._last_scroll_time = time.perf_counter() - 0.05
    canvas._on_smooth_scroll_tick()

    # 10 bars/s * 0.05s = 0.5 bars moved!
    # Initial was 499.0, single step moved to 498.0, tick moves by ~0.5 to ~497.5
    assert 497.0 < canvas.x_trans.right_index < 498.0
    # Confirm it is fractional (smooth)
    assert not canvas.x_trans.right_index.is_integer()

    # Release key -> Snap to bar!
    release_left = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    canvas.keyReleaseEvent(release_left)

    assert not canvas._key_scroll_timer.isActive()
    assert canvas._is_smooth_scrolling is False
    # Verified snap to nearest integer!
    assert canvas.x_trans.right_index.is_integer()
    assert math.isclose(canvas.x_trans.right_index, round(canvas.x_trans.right_index), abs_tol=1e-4)


def test_window_forwarding_and_focus_out(qapp):
    """Test ChartWindow forwards arrow keys to canvas and focusOutEvent stops scrolling."""
    cfg = ViewerConfig(key_scroll_step_bars=1.0)
    win = ChartWindow(window_id="test-win", config=cfg)
    win.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    win.bind_data(win_data)

    initial_right = win.canvas.x_trans.right_index

    # Dispatch to window
    event_left = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    win.keyPressEvent(event_left)

    assert math.isclose(win.canvas.x_trans.right_index, initial_right - 1.0, abs_tol=1e-4)
    assert win.canvas._key_hold_timer.isActive()

    # Trigger focusOutEvent on canvas
    focus_event = QFocusEvent(QEvent.Type.FocusOut)
    win.canvas.focusOutEvent(focus_event)

    assert not win.canvas._key_hold_timer.isActive()
    assert win.canvas._active_scroll_key is None


def test_canvas_single_tap_zoom(qapp):
    """Test single tap of Arrow Up (zoom in) and Arrow Down (zoom out)."""
    cfg = ViewerConfig(key_zoom_step_factor=1.15, key_zoom_hold_delay_ms=500)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    initial_width = canvas.x_trans.candle_width_px

    # 1. Tap Arrow Up -> Zoom in (candles get wider)
    press_up = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press_up)

    expected_up_width = initial_width * 1.15
    assert math.isclose(canvas.x_trans.candle_width_px, expected_up_width, abs_tol=1e-3)
    assert canvas._key_zoom_hold_timer.isActive()

    release_up = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    canvas.keyReleaseEvent(release_up)
    assert not canvas._key_zoom_hold_timer.isActive()
    assert not canvas._key_zoom_timer.isActive()

    # 2. Tap Arrow Down -> Zoom out (candles get narrower)
    press_down = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press_down)

    expected_down_width = expected_up_width / 1.15
    assert math.isclose(canvas.x_trans.candle_width_px, expected_down_width, abs_tol=1e-3)

    release_down = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
    canvas.keyReleaseEvent(release_down)


def test_canvas_auto_repeat_ignored_zoom(qapp):
    """Test OS auto-repeat is ignored for Arrow Up/Down zoom."""
    cfg = ViewerConfig(key_zoom_step_factor=1.15)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    initial_width = canvas.x_trans.candle_width_px

    # Normal press
    press1 = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press1)
    expected_width = initial_width * 1.15
    assert math.isclose(canvas.x_trans.candle_width_px, expected_width, abs_tol=1e-3)

    # Auto-repeat press (should be ignored)
    press_repeat = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier, "", True)
    canvas.keyPressEvent(press_repeat)

    assert math.isclose(canvas.x_trans.candle_width_px, expected_width, abs_tol=1e-3)
    canvas._stop_key_zooming()


def test_canvas_hold_smooth_zoom(qapp):
    """Test key hold > 500ms initiates smooth continuous zoom."""
    cfg = ViewerConfig(
        key_zoom_step_factor=1.15,
        key_zoom_hold_delay_ms=500,
        key_zoom_speed_per_sec=1.15,
    )
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    # Press Arrow Up
    press_up = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(press_up)
    width_after_tap = canvas.x_trans.candle_width_px

    # Trigger hold timeout
    assert canvas._key_zoom_hold_timer.isActive()
    canvas._on_key_zoom_hold_timeout()

    assert canvas._is_smooth_zooming is True
    assert canvas._key_zoom_timer.isActive()

    # Simulate smooth zoom tick (dt = 0.5s)
    canvas._last_zoom_time = time.perf_counter() - 0.5
    canvas._on_smooth_zoom_tick()

    assert canvas.x_trans.candle_width_px > width_after_tap

    # Release key
    release_up = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    canvas.keyReleaseEvent(release_up)

    assert not canvas._key_zoom_timer.isActive()
    assert canvas._is_smooth_zooming is False


def test_window_forwarding_zoom(qapp):
    """Test ChartWindow forwards Arrow Up/Down to canvas."""
    cfg = ViewerConfig(key_zoom_step_factor=1.15)
    win = ChartWindow(window_id="test-win", config=cfg)
    win.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    win.bind_data(win_data)

    initial_width = win.canvas.x_trans.candle_width_px

    event_up = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier)
    win.keyPressEvent(event_up)

    assert math.isclose(win.canvas.x_trans.candle_width_px, initial_width * 1.15, abs_tol=1e-3)
    assert win.canvas._key_zoom_hold_timer.isActive()

    # FocusOut on canvas cancels zoom timer
    focus_event = QFocusEvent(QEvent.Type.FocusOut)
    win.canvas.focusOutEvent(focus_event)
    assert not win.canvas._key_zoom_hold_timer.isActive()
    assert win.canvas._active_zoom_key is None


def test_shift_scroll_immediate_activation_and_speed(qapp):
    """Test Shift + Arrow Left immediately activates smooth scrolling with 2x multiplier."""
    cfg = ViewerConfig(key_scroll_step_bars=1.0, key_shift_speed_multiplier=2.0)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    initial_right = canvas.x_trans.right_index

    # Press Shift + Arrow Left
    press_shift_left = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Left,
        Qt.KeyboardModifier.ShiftModifier,
    )
    canvas.keyPressEvent(press_shift_left)

    # 1. Immediate activation: scroll timer is running without 500ms delay!
    assert canvas._key_scroll_timer.isActive() is True
    assert canvas._is_smooth_scrolling is True
    assert canvas._key_hold_timer.isActive() is False

    # 2. Shift multiplier (2x) applied to single step: moved 2.0 bars
    assert math.isclose(canvas.x_trans.right_index, initial_right - 2.0, abs_tol=1e-4)

    # Release key
    release_left = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    canvas.keyReleaseEvent(release_left)
    assert not canvas._key_scroll_timer.isActive()


def test_shift_zoom_immediate_activation_and_speed(qapp):
    """Test Shift + Arrow Up immediately activates smooth zoom with 2x multiplier."""
    cfg = ViewerConfig(key_zoom_step_factor=1.15, key_shift_speed_multiplier=2.0)
    canvas = ChartCanvas(window_id="test-win", config=cfg)
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000 + i * 100, t_close=1100 + i * 100, open=100.0, high=110.0, low=90.0, close=105.0)
        for i in range(500)
    ]
    canvas.set_window_data(win_data)

    initial_width = canvas.x_trans.candle_width_px

    # Press Shift + Arrow Up
    press_shift_up = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Up,
        Qt.KeyboardModifier.ShiftModifier,
    )
    canvas.keyPressEvent(press_shift_up)

    # 1. Immediate activation: zoom timer running immediately!
    assert canvas._key_zoom_timer.isActive() is True
    assert canvas._is_smooth_zooming is True
    assert canvas._key_zoom_hold_timer.isActive() is False

    # 2. Shift multiplier applied to zoom step: 1.15 ** 2.0
    expected_width = initial_width * (1.15 ** 2.0)
    assert math.isclose(canvas.x_trans.candle_width_px, expected_width, abs_tol=1e-3)

    release_up = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    canvas.keyReleaseEvent(release_up)
    assert not canvas._key_zoom_timer.isActive()

