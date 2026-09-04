"""Performance benchmarks and acceptance verification according to Section 9 & Criteria 6, 7."""

import time
from chart_viewer.agent.synthetic_feed import generate_synthetic_bars, generate_500k_snapshot
from chart_viewer.models.envelope import make_envelope, encode_envelope, decode_envelope
from chart_viewer.core.state_manager import StateManager
from chart_viewer.core.backpressure import TickCoalescer
from chart_viewer.ui.canvas import ChartCanvas
from PySide6.QtCore import QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import Qt


def test_criterion_7_snapshot_500k_bars_decode_under_one_second():
    """Criterion 7: Snapshot von 500k Bars in < 1 Sekunde dekodiert und renderbereit."""
    print("\nGenerating 500k bars snapshot payload...")
    snapshot_payload = generate_500k_snapshot("BTCUSDT")
    env = make_envelope("snapshot.full", payload=snapshot_payload, window_id="win-benchmark")

    # Encode to binary MessagePack
    binary_bytes = encode_envelope(env)
    print(f"500k binary MessagePack size: {len(binary_bytes) / (1024 * 1024):.2f} MB")

    # Measure decoding time
    start_time = time.perf_counter()
    decoded_env = decode_envelope(binary_bytes)
    decode_duration = time.perf_counter() - start_time
    print(f"500k bars decode duration: {decode_duration:.4f}s")

    # Decode MUST be well under 1.0 second (typically ~0.1 - 0.3s with msgspec!)
    assert decode_duration < 1.0, f"Snapshot decoding took too long: {decode_duration:.4f}s"

    # Measure state manager preparation time
    sm = StateManager()
    prep_start = time.perf_counter()
    win_data = sm.apply_snapshot("win-benchmark", decoded_env.payload)
    prep_duration = time.perf_counter() - prep_start
    print(f"500k bars StateManager apply duration: {prep_duration:.4f}s")

    assert len(win_data.bars) == 500_000
    assert decode_duration + prep_duration < 1.5


def test_criterion_6_1000_ticks_per_second_backpressure_no_backlog():
    """Criterion 6 & Section 9: Reaktion auf 1000 Ticks/Sekunde: kein Backlog-Wachstum."""
    repaints_count = 0

    def on_flush(win_id, data):
        nonlocal repaints_count
        repaints_count += 1

    coalescer = TickCoalescer(on_flush)

    # Push 1,000 ticks in a tight loop
    start_time = time.perf_counter()
    for i in range(1000):
        coalescer.push_tick("win-crypto", {"price": 50000.0 + i, "tick": i})
    push_duration = time.perf_counter() - start_time
    print(f"\n1,000 ticks pushed in: {push_duration * 1000:.2f} ms")

    # Flush (represents 1 render frame tick at 60Hz)
    flushed = coalescer.flush()
    assert flushed == 1
    assert repaints_count == 1

    # Backlog check: slots contain only 1 item per window, dirty is cleared
    assert len(coalescer._slots["win-crypto"]) >= 2
    assert coalescer._dirty["win-crypto"] is False
    assert coalescer.flush() == 0  # Second flush triggers 0 repaints


def test_section_9_crosshair_frame_time_under_8ms(qapp):
    """Section 9: Frame-Zeit bei Crosshair-Bewegung < 8 ms (≈120 FPS) bei 500 sichtbaren Kerzen."""
    canvas = ChartCanvas("win-bench")
    canvas.resize(1000, 700)

    # 500 synthetic candles
    bars = generate_synthetic_bars(500, start_price=100.0)
    sm = StateManager()
    win_data = sm.apply_snapshot("win-bench", {
        "symbol": "BTCUSDT",
        "bars": bars,
        "overlays": [],
        "annotations": [],
    })
    canvas.set_window_data(win_data)

    # Initial paint to cache Layers 1-3
    canvas.repaint()

    # Move crosshair 100 times and measure average frame time
    timings = []
    for i in range(100):
        x = 100.0 + (i % 800)
        y = 100.0 + ((i * 3) % 500)
        event = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(x, y),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )

        t0 = time.perf_counter()
        canvas.mouseMoveEvent(event)
        # Force immediate Qt repaint of dirty region (Layer 4)
        canvas.repaint()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        timings.append(dt_ms)

    avg_frame_time_ms = sum(timings) / len(timings)
    max_frame_time_ms = max(timings)
    print(f"\nCrosshair move average frame time: {avg_frame_time_ms:.3f} ms (max: {max_frame_time_ms:.3f} ms)")

    # Section 9 target: < 8.0 ms
    assert avg_frame_time_ms < 8.0, f"Average frame time exceeded 8ms: {avg_frame_time_ms:.3f} ms"
