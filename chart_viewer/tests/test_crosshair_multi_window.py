import pytest

from chart_viewer.config import ViewerConfig
"""End-to-end integration test for Criterion 2: Multi-Window Crosshair Sync & Downtime Clamping."""

from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.agent.synthetic_feed import generate_synthetic_bars
from chart_viewer.transport.in_process import create_in_process_pair
from PySide6.QtCore import QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import Qt


def test_criterion_2_multi_window_crosshair_sync_with_downtime_clamping(qapp):
    """Criterion 2: Zwei Fenster (Daily + 5-Min, Crypto-Symbol) synchronisieren das Crosshair

    arithmetisch korrekt, inkl. Clamping über eine Exchange-Downtime hinweg.
    """
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)

    agent.start()
    app.start()

    # Window A: Daily window, group "crypto-btc"
    agent.open_window(
        window_id="win-daily",
        symbol="BTCUSDT",
        timeframe_unit="D",
        multiplier=1,
        sync_group_id="crypto-btc",
    )

    # Window B: 5-Min window, group "crypto-btc"
    agent.open_window(
        window_id="win-5m",
        symbol="BTCUSDT",
        timeframe_unit="min",
        multiplier=5,
        sync_group_id="crypto-btc",
    )
    qapp.processEvents()

    assert "win-daily" in app.windows
    assert "win-5m" in app.windows

    win_daily = app.windows["win-daily"]
    win_5m = app.windows["win-5m"]

    # Generate bars:
    # 5-min bars with a downtime gap at bar 10 spanning 10 missing bars
    bars_5m = generate_synthetic_bars(
        count=50,
        start_price=100.0,
        start_time=1700000000,
        bar_duration_sec=300,
        downtime_gap_at=10,
        downtime_gap_bars=10,  # Missing 10 * 300 = 3000 seconds
    )

    agent.send_snapshot("win-5m", {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "min", "multiplier": 5},
        "sync_group_id": "crypto-btc",
        "bars": bars_5m,
    })

    # Daily bars covering the same timeframe
    bars_daily = generate_synthetic_bars(
        count=10,
        start_price=100.0,
        start_time=1700000000,
        bar_duration_sec=86400,
    )
    agent.send_snapshot("win-daily", {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "D", "multiplier": 1},
        "sync_group_id": "crypto-btc",
        "bars": bars_daily,
    })
    qapp.processEvents()

    # Move mouse in Window Daily
    # Pick timestamp falling inside the downtime gap of Window B
    # Gap in 5m bars is between bar 9 and bar 10:
    # bar 9 t_open = 1700000000 + 9*300 = 1700002700
    # bar 10 t_open = 1700002700 + 10*300 = 1700005700
    gap_target_ts = 1700004000  # right in the middle of missing exchange downtime

    # Broadcast crosshair from Daily window
    app.event_hub.broadcast_crosshair(
        source_window_id="win-daily",
        timestamp=gap_target_ts,
        source_sync_group_id="crypto-btc",
    )
    qapp.processEvents()

    # Verify Window B clamped to the closest available bar and crosshair did NOT disappear!
    main_pane_b = win_5m.canvas._panes["main"]
    assert main_pane_b._crosshair_x is not None, (
        "Crosshair should NOT disappear over exchange downtime gap!"
    )


def _new_pair():
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    return agent, app


def _open_5m(agent, app, qapp, window_id, group, bars):
    agent.open_window(
        window_id=window_id,
        symbol="BTCUSDT",
        timeframe_unit="min",
        multiplier=5,
        sync_group_id=group,
    )
    snap = {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "min", "multiplier": 5},
        "bars": bars,
    }
    if group is not None:
        snap["sync_group_id"] = group
    agent.send_snapshot(window_id, snap)
    qapp.processEvents()
    return app.windows[window_id]


def test_crosshair_lands_on_correct_bar_after_downtime_gap(qapp):
    """Regression: nearest-neighbour clamp auf einen Bar NACH der Lücke muss
    indexbasiert landen, nicht über (ts - t0)/duration naiv zurückgerechnet.

    Ziel (win-b): 5-min, Lücke von 10 Bars VOR Bar 10.
      bar 9  t_open = 1700002700
      bar 10 t_open = 1700005700 (nach Lücke)
    Broadcast 1700005000 liegt näher an bar 10 -> Ziel muss auf Index 10 landen.
    Der naive Weg rechnet (1700005700 - 1700000000)/300 = 19.0 -> falsch.
    """
    agent, app = _new_pair()

    bars_uniform = generate_synthetic_bars(
        count=50, start_price=100.0, start_time=1700000000, bar_duration_sec=300,
    )
    _open_5m(agent, app, qapp, "win-a", "g", bars_uniform)

    bars_gapped = generate_synthetic_bars(
        count=50, start_price=100.0, start_time=1700000000, bar_duration_sec=300,
        downtime_gap_at=10, downtime_gap_bars=10,
    )
    win_b = _open_5m(agent, app, qapp, "win-b", "g", bars_gapped)

    app.event_hub.broadcast_crosshair(
        source_window_id="win-a", timestamp=1700005000, source_sync_group_id="g",
    )
    qapp.processEvents()

    main_pane = win_b.canvas._panes["main"]
    expected_x = win_b.canvas.x_trans.bar_to_x(10.0)
    assert main_pane._crosshair_x == pytest.approx(expected_x, abs=0.5), (
        f"Crosshair soll auf Bar-Index 10 landen, ist aber bei x={main_pane._crosshair_x} "
        f"(erwartet {expected_x})"
    )


def test_crosshair_sync_with_millisecond_timestamps(qapp):
    """Regression: Bars mit ms-Timestamps werden intern auf Sekunden normalisiert
    und syncen korrekt (kein Sekunden/ms-Mix beim Index-Rückweg).
    """
    agent, app = _new_pair()

    bars_ms = generate_synthetic_bars(
        count=20, start_price=100.0, start_time=1700000000000, bar_duration_sec=300000,
    )
    _open_5m(agent, app, qapp, "win-a", "g", bars_ms)
    win_b = _open_5m(agent, app, qapp, "win-b", "g", bars_ms)

    # Normalisierung: ms -> Sekunden an der Datenkante
    b_data = app.state_manager.get_window_data("win-b")
    assert b_data.bars[5].t_open == 1700001500

    # Broadcast mit dem normalisierten Sekunden-Timestamp von Bar 5
    app.event_hub.broadcast_crosshair(
        source_window_id="win-a", timestamp=1700001500, source_sync_group_id="g",
    )
    qapp.processEvents()

    main_pane = win_b.canvas._panes["main"]
    expected_x = win_b.canvas.x_trans.bar_to_x(5.0)
    assert main_pane._crosshair_x == pytest.approx(expected_x, abs=0.5), (
        f"Crosshair soll bei ms-Daten auf Bar-Index 5 landen, ist aber bei "
        f"x={main_pane._crosshair_x} (erwartet {expected_x})"
    )


def test_crosshair_sync_when_group_only_in_snapshot(qapp):
    """Regression: sync_group_id darf nicht nur im window.open-Register leben.
    Wird die Gruppe erst über den Snapshot geliefert, muss das Fenster trotzdem syncen.
    """
    agent, app = _new_pair()
    bars = generate_synthetic_bars(
        count=30, start_price=100.0, start_time=1700000000, bar_duration_sec=300,
    )

    _open_5m(agent, app, qapp, "win-a", "g", bars)

    # win-b: OHNE Gruppe geöffnet, Gruppe kommt erst über den Snapshot
    agent.open_window(
        window_id="win-b", symbol="BTCUSDT", timeframe_unit="min", multiplier=5,
        sync_group_id=None,
    )
    agent.send_snapshot("win-b", {
        "symbol": "BTCUSDT",
        "timeframe": {"unit": "min", "multiplier": 5},
        "sync_group_id": "g",
        "bars": bars,
    })
    qapp.processEvents()
    win_b = app.windows["win-b"]

    app.event_hub.broadcast_crosshair(
        source_window_id="win-a", timestamp=1700000000 + 5 * 300, source_sync_group_id="g",
    )
    qapp.processEvents()

    main_pane = win_b.canvas._panes["main"]
    assert main_pane._crosshair_x is not None, (
        "Fenster mit Gruppe-über-Snapshot muss Crosshair-Sync empfangen"
    )


def test_ungrouped_windows_do_not_cross_sync(qapp):
    """Regression: Zwei ungruppierte Fenster (beide ohne sync_group_id) dürfen
    NICHT implizit über None == None miteinander syncen.
    """
    agent, app = _new_pair()
    bars = generate_synthetic_bars(
        count=30, start_price=100.0, start_time=1700000000, bar_duration_sec=300,
    )

    for wid in ("win-a", "win-b"):
        agent.open_window(
            window_id=wid, symbol="BTCUSDT", timeframe_unit="min", multiplier=5,
            sync_group_id=None,
        )
        agent.send_snapshot(wid, {
            "symbol": "BTCUSDT",
            "timeframe": {"unit": "min", "multiplier": 5},
            "bars": bars,
        })
    qapp.processEvents()
    win_b = app.windows["win-b"]

    app.event_hub.broadcast_crosshair(
        source_window_id="win-a", timestamp=1700000000 + 5 * 300,
    )
    qapp.processEvents()

    main_pane = win_b.canvas._panes["main"]
    assert main_pane._crosshair_x is None, (
        "Ungruppierte Fenster dürfen nicht cross-syncen (kein None==None-Matching)"
    )


def test_remote_crosshair_is_vertical_only(qapp):
    """Regression: Inter-Window-Sync setzt nur die vertikale Linie.
    Das Ziel darf keine horizontale Linie (aktives Pane) erzwingen.
    """
    agent, app = _new_pair()
    bars = generate_synthetic_bars(
        count=30, start_price=100.0, start_time=1700000000, bar_duration_sec=300,
    )
    _open_5m(agent, app, qapp, "win-a", "g", bars)
    win_b = _open_5m(agent, app, qapp, "win-b", "g", bars)

    app.event_hub.broadcast_crosshair(
        source_window_id="win-a", timestamp=1700000000 + 5 * 300, source_sync_group_id="g",
    )
    qapp.processEvents()

    main_pane = win_b.canvas._panes["main"]
    assert main_pane._crosshair_x is not None, "vertikale Linie muss gesetzt sein"
    assert main_pane._crosshair_y is None, (
        "Remote-Sync darf keine horizontale Linie zeichnen (kein aktives Pane)"
    )
