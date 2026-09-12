from chart_viewer.config import ViewerConfig
"""Regression tests: live watchlist updates must reach the viewer.

Two defects are covered here — before the fix both silently did nothing while the
agent still reported success:

1. ``watchlist.data`` (partial payload ``{replace, columns, rows}``) was decoded as a
   full ``WatchlistState`` (which requires ``list_id`` + ``display_name``), raised
   inside the Qt slot and therefore never applied data nor sent an ack.
2. Re-sending ``watchlist.open`` for an already existing window only moved/resized it;
   the new rows were dropped because ``set_data()`` was never called.
"""

from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.transport.in_process import create_in_process_pair


LIST_ID = "current_positions"


def _rows(symbols: list[str]) -> list[dict]:
    return [{"symbol": s, "cells": {"Symbol": s}} for s in symbols]


def _symbols_in_table(win) -> list[str]:
    symbols = []
    for row in range(win.table.rowCount()):
        item = win.table.item(row, 0)
        symbols.append(item.text() if item is not None else "")
    return symbols


def _setup(qapp):
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)
    agent.start()
    app.start()
    return agent, app


def test_partial_watchlist_data_update_is_applied_and_acked(qapp):
    """A partial watchlist.data payload updates columns/rows and is acked."""
    agent, app = _setup(qapp)

    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["AMZN", "ARM"]),
    )
    qapp.processEvents()
    assert _symbols_in_table(app.watchlists[LIST_ID]) == ["AMZN", "ARM"]

    agent.received_events.clear()

    # Partial update — exactly what run_server sends for UPDATE_DATA.
    agent.update_watchlist_data(
        list_id=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["DELL", "GLW", "VLO"]),
        replace=True,
    )
    qapp.processEvents()

    # Bug 1: the rows must actually arrive in the table.
    assert _symbols_in_table(app.watchlists[LIST_ID]) == ["DELL", "GLW", "VLO"]

    # And the viewer must ack, otherwise the update failed silently.
    acks = [e for e in agent.received_events if e.type == "ack"]
    assert len(acks) == 1, "watchlist.data was not acked by the viewer"


def test_partial_watchlist_update_merges_when_replace_is_false(qapp):
    """replace=False merges by symbol instead of replacing the whole list."""
    agent, app = _setup(qapp)

    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["AMZN", "ARM"]),
    )
    qapp.processEvents()

    agent.update_watchlist_data(
        list_id=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["DELL"]),
        replace=False,
    )
    qapp.processEvents()

    assert sorted(_symbols_in_table(app.watchlists[LIST_ID])) == ["AMZN", "ARM", "DELL"]


def test_unknown_watchlist_data_is_ignored_without_crashing(qapp):
    """watchlist.data for an unknown window must not raise and must still ack."""
    agent, app = _setup(qapp)

    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["AMZN"]),
    )
    qapp.processEvents()
    agent.received_events.clear()

    agent.update_watchlist_data(
        list_id="wl_does_not_exist",
        columns=["Symbol"],
        rows=_rows(["NVDA"]),
        replace=True,
    )
    qapp.processEvents()

    # Existing watchlist untouched, no exception, ack sent.
    assert _symbols_in_table(app.watchlists[LIST_ID]) == ["AMZN"]
    assert [e for e in agent.received_events if e.type == "ack"]


def test_resent_watchlist_open_refreshes_existing_window_content(qapp):
    """Bug 2: re-opening an existing watchlist window must refresh its content."""
    agent, app = _setup(qapp)

    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["AMZN", "ARM"]),
        position={"x": 923, "y": 122},
        size={"width": 117, "height": 873},
    )
    qapp.processEvents()

    win = app.watchlists[LIST_ID]
    assert _symbols_in_table(win) == ["AMZN", "ARM"]

    # Second push with new content (e.g. after LOAD_SETUP created an empty window).
    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["DELL", "GLW", "VLO", "WULF"]),
        position={"x": 923, "y": 122},
        size={"width": 117, "height": 873},
    )
    qapp.processEvents()

    assert _symbols_in_table(app.watchlists[LIST_ID]) == ["DELL", "GLW", "VLO", "WULF"]
    assert len(app.watchlists) == 1, "existing window must be reused, not duplicated"


def test_empty_columns_watchlist_stays_empty_without_raising(qapp):
    """LOAD_SETUP opens watchlists with columns=[] and rows=[] — that must be tolerated."""
    agent, app = _setup(qapp)

    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=[],
        rows=[],
        position={"x": 923, "y": 122},
        size={"width": 117, "height": 873},
    )
    qapp.processEvents()

    win = app.watchlists[LIST_ID]
    assert win.table.rowCount() == 0

    # Filling it afterwards via a partial update must work on that window too.
    agent.update_watchlist_data(
        list_id=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["AMZN", "ARM"]),
        replace=True,
    )
    qapp.processEvents()

    assert _symbols_in_table(win) == ["AMZN", "ARM"]


def test_load_setup_style_reopen_keeps_existing_content(qapp):
    """LOAD_SETUP reopens watchlists with columns=[]/rows=[] — that must NOT wipe content.

    run_server.py's LOAD_SETUP reuses existing watchlist windows by re-sending
    watchlist.open with empty columns/rows (a setup stores geometry only). Refreshing
    unconditionally would clear a populated watchlist just because a layout was loaded.
    """
    agent, app = _setup(qapp)

    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=["Symbol"],
        rows=_rows(["AMZN", "ARM", "DELL"]),
        position={"x": 923, "y": 122},
        size={"width": 117, "height": 873},
    )
    qapp.processEvents()
    assert _symbols_in_table(app.watchlists[LIST_ID]) == ["AMZN", "ARM", "DELL"]

    # LOAD_SETUP reuse path: same window id, empty payload, new geometry.
    agent.open_watchlist(
        list_id=LIST_ID,
        display_name=LIST_ID,
        columns=[],
        rows=[],
        position={"x": 100, "y": 100},
        size={"width": 300, "height": 400},
    )
    qapp.processEvents()

    assert _symbols_in_table(app.watchlists[LIST_ID]) == ["AMZN", "ARM", "DELL"], \
        "loading a layout must not wipe a populated watchlist"
    # Geometry still applied.
    assert app.watchlists[LIST_ID].width() == 300
