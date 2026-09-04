"""Tests for interaction model and ephemeral measure tool (Section 7 & Criterion 3)."""

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QMouseEvent
from chart_viewer.ui.canvas import ChartCanvas
from chart_viewer.ui.interaction.state_machine import InteractionStateMachine, InteractionState
from chart_viewer.core.state_manager import WindowData
from chart_viewer.models.entities import Bar, Annotation, Anchor


def test_state_machine_measuring_ephemeral():
    """Verify Criterion 3: Measure tool appears on drag, disappears on release, never persisted."""
    sm = InteractionStateMachine()
    assert sm.state == InteractionState.IDLE

    # Start measuring
    sm.start_measuring()
    assert sm.state == InteractionState.MEASURING

    # Mouse release
    prior, touched_ann = sm.release()
    assert prior == InteractionState.MEASURING
    assert touched_ann is None
    assert sm.state == InteractionState.IDLE


def test_state_machine_escape_cancels_immediately():
    sm = InteractionStateMachine()
    sm.start_measuring()
    assert sm.state == InteractionState.MEASURING

    sm.cancel()
    assert sm.state == InteractionState.IDLE


def test_canvas_measure_tool_lifecycle(qapp):
    """Test canvas mouse interaction for measure tool lifecycle."""
    canvas = ChartCanvas(window_id="test-win")
    canvas.resize(800, 600)

    win_data = WindowData("test-win")
    win_data.bars = [
        Bar(t_open=1000, t_close=2000, open=100.0, high=110.0, low=90.0, close=105.0),
        Bar(t_open=2000, t_close=3000, open=105.0, high=115.0, low=95.0, close=110.0),
    ]
    canvas.set_window_data(win_data)

    # 1. Simulate mouse press on empty chart (e.g. at 200, 300)
    press_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(200, 300),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.mousePressEvent(press_event)

    assert canvas.sm.state == InteractionState.MEASURING
    assert canvas.layer4_interaction.is_measuring is True
    assert canvas.layer4_interaction.measure_start_pos == QPointF(200, 300)

    # 2. Simulate mouse move (measuring active)
    move_event = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        QPointF(350, 200),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.mouseMoveEvent(move_event)
    assert canvas.layer4_interaction.is_measuring is True

    # 3. Simulate mouse release -> MUST immediately discard measurement and never persist
    release_event = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(350, 200),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.mouseReleaseEvent(release_event)

    assert canvas.sm.state == InteractionState.IDLE
    assert canvas.layer4_interaction.is_measuring is False
    assert canvas.layer4_interaction.measure_start_pos is None
    # Verify no persistent annotations were added
    assert len(win_data.annotations) == 0
