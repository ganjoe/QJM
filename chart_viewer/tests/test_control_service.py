"""Tests for the agent-side control service (control panel backend)."""

import time
from typing import Any, Dict, List

import pytest

from chart_viewer.agent.control_service import (
    ControlConfig,
    ControlError,
    ControlService,
    clean_list_name,
    is_master_universe,
    sanitize_query,
    sanitize_ticker,
)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    def send_command(self, envelope) -> None:
        self.sent.append(envelope)


class FakeAgent:
    """Minimal ChartAgent stand-in: transport, ledger and watchlist refresh hook."""

    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.layout_ledger: Dict[str, dict] = {}
        self.watchlist_updates: List[dict] = []

    def update_watchlist_data(self, list_id, columns=None, rows=None, replace=False) -> None:
        self.watchlist_updates.append(
            {"list_id": list_id, "columns": columns, "rows": rows, "replace": replace}
        )


def make_service(**overrides) -> tuple:
    agent = FakeAgent()
    config = ControlConfig(
        pca_url="http://pca.test:8791",
        control_api_url="http://control.test:8766",
        stock_data_url="http://stock.test:8002",
        http_timeout_s=1.0,
        search_cache_ttl_s=30.0,
        **overrides,
    )
    return ControlService(agent, config=config), agent


def wait_for_response(agent: FakeAgent, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if agent.transport.sent:
            return agent.transport.sent[-1]
        time.sleep(0.01)
    raise AssertionError("control service did not answer in time")


# ── helpers ────────────────────────────────────────────────────────────────


def test_query_and_ticker_sanitizing():
    assert sanitize_query("nv; drop% table") == "NVDROPTABLE"
    assert sanitize_query("  brk.b ") == "BRK.B"
    assert sanitize_query("a" * 40) == "A" * 12
    assert sanitize_ticker(" $nvda! ") == "NVDA"
    assert clean_list_name("00_positions; drop") == "00_positions drop"
    assert is_master_universe("ALL")
    assert is_master_universe("all.txt")
    assert not is_master_universe("scan_latest")


# ── symbol search ──────────────────────────────────────────────────────────


def test_search_symbols_builds_postgrest_prefix_query_and_caches():
    service, agent = make_service()
    calls: List[str] = []

    def fake_get(path: str):
        calls.append(path)
        return [
            {"ticker": "NVDA", "type": "CS", "has_parquet": True},
            {"ticker": "NVD", "type": "ETF", "has_parquet": False},
        ]

    service._supabase_get = fake_get
    service.handle("req-1", "search_symbols", {"query": "nv", "limit": 20})

    response = agent.transport.sent[-1]
    assert response.type == "control.response"
    assert response.payload["request_id"] == "req-1"
    assert response.payload["ok"] is True
    assert response.payload["error"] is None

    data = response.payload["data"]
    assert [r["ticker"] for r in data["results"]] == ["NVD", "NVDA"]  # alphabetical
    assert data["results"][1]["has_parquet"] is True
    assert len(calls) == 1
    assert "/cda_master_universe?" in calls[0]
    assert "select=ticker,type,has_parquet" in calls[0]
    assert "ticker=ilike.NV%25" in calls[0]
    assert "order=ticker.asc" in calls[0]
    assert "limit=20" in calls[0]

    # Same query again -> served from the LRU cache, no second backend call
    service.handle("req-2", "search_symbols", {"query": "nv", "limit": 20})
    assert len(calls) == 1
    assert agent.transport.sent[-1].payload["data"]["results"][1]["ticker"] == "NVDA"


def test_search_symbols_hoists_exact_match_and_falls_back_to_contains():
    service, agent = make_service()
    patterns: List[str] = []

    def fake_get(path: str):
        patterns.append(path)
        if "%25" in path and path.count("%25") >= 2:  # contains query
            return [{"ticker": "QMAGGI", "type": "CS", "has_parquet": True}]
        return []

    service._supabase_get = fake_get
    service.handle("req-3", "search_symbols", {"query": "maggi"})

    data = agent.transport.sent[-1].payload["data"]
    assert [r["ticker"] for r in data["results"]] == ["QMAGGI"]
    assert len(patterns) == 2
    assert "ticker=ilike.MAGGI%25" in patterns[0]
    assert "ticker=ilike.%25MAGGI%25" in patterns[1]


def test_search_symbols_empty_query_returns_no_backend_call():
    service, agent = make_service()
    service._supabase_get = lambda path: pytest.fail("backend must not be called")
    service.handle("req-4", "search_symbols", {"query": "   "})
    data = agent.transport.sent[-1].payload["data"]
    assert data == {"results": [], "match_count": 0, "source": "empty"}


def test_unknown_op_is_rejected():
    service, agent = make_service()
    service.handle("req-5", "drop_everything", {})
    payload = agent.transport.sent[-1].payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_request"


# ── watchlists ─────────────────────────────────────────────────────────────


def test_list_watchlists_puts_master_first():
    service, agent = make_service()
    service._pca_json = lambda method, path, payload=None: {
        "watchlists": ["scan_latest", "all", "10_favorite"]
    }
    service.handle("req-6", "list_watchlists", {})
    data = wait_for_response(agent).payload["data"]
    assert data["watchlists"][0] == {"name": "all", "editable": False}
    assert [w["name"] for w in data["watchlists"]] == ["all", "10_favorite", "scan_latest"]
    assert all(w["editable"] for w in data["watchlists"][1:])


def test_get_watchlist_returns_tickers_sorted_by_position():
    service, agent = make_service()
    service._pca_json = lambda method, path, payload=None: {
        "list_name": "10_favorite",
        "tickers": [
            {"ticker": "HOOD", "position": 999},
            {"ticker": "VICR", "position": 0},
        ],
    }
    service.handle("req-7", "get_watchlist", {"list_name": "10_favorite"})
    data = wait_for_response(agent).payload["data"]
    assert [t["ticker"] for t in data["tickers"]] == ["VICR", "HOOD"]
    assert data["count"] == 2
    assert data["editable"] is True
    assert data["truncated"] is False


def test_get_watchlist_master_is_truncated_and_readonly():
    service, agent = make_service()
    service._supabase_count = lambda path: 41723
    service._pca_json = lambda *a, **k: pytest.fail("PCA service must not be called for 'all'")

    service.handle("req-8", "get_watchlist", {"list_name": "all"})
    data = wait_for_response(agent).payload["data"]
    assert data == {
        "list_name": "all",
        "tickers": [],
        "count": 41723,
        "editable": False,
        "truncated": True,
    }


def test_mutate_watchlist_add_maps_already_exists_and_triggers_download():
    service, agent = make_service()
    calls: List[tuple] = []
    downloaded: List[list] = []

    def fake_pca(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            return {"status": "already_exists", "ticker": "NVDA"}
        return {"list_name": "10_favorite", "tickers": [{"ticker": "NVDA", "position": 0}]}

    service._pca_json = fake_pca
    service._trigger_download = lambda tickers: downloaded.append(list(tickers))
    service.handle("req-9", "mutate_watchlist", {"action": "add", "list_name": "10_favorite", "ticker": "nvda"})

    data = wait_for_response(agent).payload["data"]
    assert data["status"] == "already_exists"
    assert data["count"] == 1
    assert calls[0] == (
        "POST",
        "/api/watchlists",
        {"list_name": "10_favorite", "ticker": "NVDA", "position": 0},
    )
    assert downloaded == [["NVDA"]]


def test_mutate_watchlist_remove_uses_delete_endpoint():
    service, agent = make_service()
    calls: List[tuple] = []
    service._pca_json = lambda method, path, payload=None: calls.append((method, path, payload)) or {
        "list_name": "10_favorite",
        "tickers": [],
    }
    service.handle("req-10", "mutate_watchlist", {"action": "remove", "list_name": "10_favorite", "ticker": "HOOD"})
    data = wait_for_response(agent).payload["data"]
    assert data["status"] == "removed"
    assert calls[0] == ("DELETE", "/api/watchlists/10_favorite/HOOD", None)


def test_mutate_master_list_is_protected():
    service, agent = make_service()
    service.handle("req-11", "mutate_watchlist", {"action": "add", "list_name": "all", "ticker": "NVDA"})
    payload = wait_for_response(agent).payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "protected_list"


def test_mutation_refreshes_open_watchlist_window():
    service, agent = make_service()
    agent.layout_ledger["10_favorite"] = {"list_id": "10_favorite", "columns": ["Symbol"], "rows": []}
    service._pca_json = lambda method, path, payload=None: {
        "list_name": "10_favorite",
        "tickers": [{"ticker": "NVDA", "position": 0}, {"ticker": "HOOD", "position": 1}],
    }
    service._trigger_download = lambda tickers: None
    service.handle("req-12", "mutate_watchlist", {"action": "add", "list_name": "10_favorite", "ticker": "NVDA"})
    wait_for_response(agent)

    assert len(agent.watchlist_updates) == 1
    update = agent.watchlist_updates[0]
    assert update["list_id"] == "10_favorite"
    assert update["replace"] is True
    assert [r["symbol"] for r in update["rows"]] == ["NVDA", "HOOD"]


def _stateful_pca(state: Dict[str, list]):
    """Fake PCA HTTP layer backed by a dict of list_name -> tickers."""

    def fake_pca(method: str, path: str, payload=None):
        if method == "POST" and path == "/api/watchlists":
            tickers = state.setdefault(payload["list_name"], [])
            if payload["ticker"] in tickers:
                return {"status": "already_exists"}
            tickers.append(payload["ticker"])
            return {"status": "added"}
        if method == "POST" and path == "/api/watchlists/batch":
            state[payload["list_name"]] = list(payload["tickers"])
            return {"status": "saved", "count": len(payload["tickers"])}
        if method == "DELETE" and path.count("/") == 4:  # /api/watchlists/<list>/<ticker>
            list_name, ticker = path.rsplit("/", 2)[-2:]
            if ticker in state.get(list_name, []):
                state[list_name].remove(ticker)
            return {"status": "removed"}
        if method == "DELETE":  # /api/watchlists/<list>
            state.pop(path.rsplit("/", 1)[-1], None)
            return {"status": "deleted"}
        if method == "GET" and path.startswith("/api/watchlists/"):
            list_name = path.rsplit("/", 1)[-1]
            return {
                "list_name": list_name,
                "tickers": [
                    {"ticker": t, "position": i} for i, t in enumerate(state.get(list_name, []))
                ],
            }
        return {}

    return fake_pca


def test_mutate_watchlist_copy_targets_the_other_list_only():
    service, agent = make_service()
    state = {"10_favorite": ["VICR", "HOOD"], "20_watchlist": []}
    calls: List[tuple] = []
    service._pca_json = lambda method, path, payload=None: calls.append((method, path, payload)) or _stateful_pca(state)(method, path, payload)
    service._trigger_download = lambda tickers: None

    service.handle(
        "req-copy",
        "mutate_watchlist",
        {"action": "copy", "list_name": "10_favorite", "ticker": "vicr", "target_list": "20_watchlist"},
    )
    data = wait_for_response(agent).payload["data"]

    assert data["status"] == "added"
    assert data["target_list"] == "20_watchlist"
    assert data["count"] == 2, "the source list is unchanged"
    assert data["target_count"] == 1
    assert state == {"10_favorite": ["VICR", "HOOD"], "20_watchlist": ["VICR"]}
    assert (
        "POST",
        "/api/watchlists",
        {"list_name": "20_watchlist", "ticker": "VICR", "position": 0},
    ) in calls


def test_mutate_watchlist_move_adds_to_target_and_deletes_from_source():
    service, agent = make_service()
    state = {"10_favorite": ["VICR", "HOOD"], "20_watchlist": []}
    calls: List[tuple] = []
    service._pca_json = lambda method, path, payload=None: calls.append((method, path, payload)) or _stateful_pca(state)(method, path, payload)
    service._trigger_download = lambda tickers: None

    service.handle(
        "req-move",
        "mutate_watchlist",
        {"action": "move", "list_name": "10_favorite", "ticker": "VICR", "target_list": "20_watchlist"},
    )
    data = wait_for_response(agent).payload["data"]

    assert data["status"] == "moved"
    assert data["count"] == 1
    assert data["target_count"] == 1
    assert state == {"10_favorite": ["HOOD"], "20_watchlist": ["VICR"]}
    assert ("DELETE", "/api/watchlists/10_favorite/VICR", None) in calls


def test_mutate_watchlist_copy_reports_already_exists():
    service, agent = make_service()
    state = {"10_favorite": ["VICR"], "20_watchlist": ["VICR"]}
    service._pca_json = _stateful_pca(state)
    service._trigger_download = lambda tickers: None

    service.handle(
        "req-copy2",
        "mutate_watchlist",
        {"action": "copy", "list_name": "10_favorite", "ticker": "VICR", "target_list": "20_watchlist"},
    )
    data = wait_for_response(agent).payload["data"]
    assert data["status"] == "already_exists"
    assert state["20_watchlist"] == ["VICR"]


def test_mutate_watchlist_copy_list_uses_batch_replace():
    service, agent = make_service()
    state = {"10_favorite": ["VICR", "HOOD"]}
    calls: List[tuple] = []
    service._pca_json = lambda method, path, payload=None: calls.append((method, path, payload)) or _stateful_pca(state)(method, path, payload)

    service.handle(
        "req-copylist",
        "mutate_watchlist",
        {"action": "copy_list", "list_name": "10_favorite", "target_list": "30_copy"},
    )
    data = wait_for_response(agent).payload["data"]

    assert data["status"] == "copied"
    assert data["target_list"] == "30_copy"
    assert data["count"] == 2
    assert state["30_copy"] == ["VICR", "HOOD"]
    assert (
        "POST",
        "/api/watchlists/batch",
        {"list_name": "30_copy", "tickers": ["VICR", "HOOD"], "replace": True},
    ) in calls


def test_mutate_watchlist_copy_list_with_empty_source_is_rejected():
    service, agent = make_service()
    service._pca_json = lambda method, path, payload=None: {"list_name": "10_favorite", "tickers": []}
    service.handle(
        "req-copylist-empty",
        "mutate_watchlist",
        {"action": "copy_list", "list_name": "10_favorite", "target_list": "30_copy"},
    )
    payload = wait_for_response(agent).payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "empty_source"


def test_mutate_watchlist_delete_removes_the_whole_list():
    service, agent = make_service()
    calls: List[tuple] = []
    service._pca_json = lambda method, path, payload=None: calls.append((method, path, payload)) or {"status": "deleted"}

    service.handle("req-delete", "mutate_watchlist", {"action": "delete", "list_name": "10_favorite"})
    data = wait_for_response(agent).payload["data"]

    assert data == {"status": "deleted", "list_name": "10_favorite"}
    assert calls == [("DELETE", "/api/watchlists/10_favorite", None)]


def test_delete_watchlist_clears_and_forgets_an_open_window():
    service, agent = make_service()
    agent.layout_ledger["10_favorite"] = {
        "list_id": "10_favorite",
        "columns": ["Symbol"],
        "rows": [{"symbol": "VICR"}],
    }
    service._pca_json = lambda method, path, payload=None: {"status": "deleted"}

    service.handle("req-delete-open", "mutate_watchlist", {"action": "delete", "list_name": "10_favorite"})
    wait_for_response(agent)

    assert "10_favorite" not in agent.layout_ledger
    assert agent.watchlist_updates[-1]["list_id"] == "10_favorite"
    assert agent.watchlist_updates[-1]["rows"] == []
    assert agent.watchlist_updates[-1]["replace"] is True


@pytest.mark.parametrize(
    "params, code",
    [
        ({"action": "wipe", "list_name": "10_favorite", "ticker": "NVDA"}, "invalid_request"),
        ({"action": "add", "list_name": "10_favorite"}, "invalid_request"),
        ({"action": "add", "list_name": "", "ticker": "NVDA"}, "invalid_request"),
        ({"action": "copy", "list_name": "10_favorite", "ticker": "NVDA"}, "invalid_request"),
        (
            {"action": "copy", "list_name": "10_favorite", "ticker": "NVDA", "target_list": "10_favorite"},
            "invalid_request",
        ),
        (
            {"action": "copy", "list_name": "10_favorite", "ticker": "NVDA", "target_list": "all"},
            "protected_list",
        ),
        (
            {"action": "copy_list", "list_name": "10_favorite", "target_list": "all"},
            "protected_list",
        ),
        ({"action": "delete", "list_name": "all"}, "protected_list"),
        ({"action": "move", "list_name": "all", "ticker": "NVDA", "target_list": "20_x"}, "protected_list"),
    ],
)
def test_mutate_watchlist_rejects_invalid_actions(params, code):
    service, agent = make_service()
    service._pca_json = lambda *a, **k: pytest.fail("backend must not be called for invalid input")
    service.handle("req-bad", "mutate_watchlist", params)
    payload = wait_for_response(agent).payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == code


# ── presets ────────────────────────────────────────────────────────────────


def test_list_presets_normalizes_dict_payload():
    service, agent = make_service()
    service._pca_json = lambda method, path, payload=None: {
        "presets": {
            "qmaggi": {"display_name": "QMaggi", "description": "x", "indicator_count": 10},
            "default": {"display_name": "Standard"},
        }
    }
    service.handle("req-13", "list_presets", {})
    data = wait_for_response(agent).payload["data"]
    assert [p["id"] for p in data["presets"]] == ["default", "qmaggi"]
    assert data["presets"][1]["display_name"] == "QMaggi"
    assert data["count"] == 2


def test_apply_preset_only_touches_chart_windows_of_flag():
    service, agent = make_service()
    agent.layout_ledger = {
        "win_amd_1d": {"symbol": "AMD", "color_flag": 0, "timeframe": {"unit": "D", "multiplier": 1}},
        "win_msft_1d": {"symbol": "MSFT", "color_flag": 1, "timeframe": {"unit": "D", "multiplier": 1}},
        "10_favorite": {"list_id": "10_favorite", "columns": ["Symbol"], "rows": []},
    }
    posted: List[tuple] = []
    service._http_json = lambda method, url, payload=None, **kw: posted.append((method, url, payload)) or {}

    service.handle("req-14", "apply_preset", {"preset_id": "qmaggi", "color_flag": 0})
    data = wait_for_response(agent).payload["data"]
    assert data["applied"] == ["win_amd_1d"]
    assert data["preset_id"] == "qmaggi"
    assert len(posted) == 1
    method, url, payload = posted[0]
    assert method == "POST"
    assert url == "http://control.test:8766/api/command"
    assert payload["action"] == "DISPLAY_STOCK"
    assert payload["window_id"] == "win_amd_1d"
    assert payload["preset"] == "qmaggi"
    assert payload["timeframe_str"] == "1D"


def test_apply_preset_without_open_window_is_not_an_error():
    service, agent = make_service()
    service._http_json = lambda *a, **k: pytest.fail("no chart window -> no command")
    service.handle("req-15", "apply_preset", {"preset_id": "qmaggi", "color_flag": 2})
    data = wait_for_response(agent).payload["data"]
    assert data["applied"] == []
    assert data["skipped"] == []


def test_apply_preset_requires_preset_id():
    service, agent = make_service()
    service.handle("req-16", "apply_preset", {"color_flag": 0})
    payload = wait_for_response(agent).payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_request"


def test_backend_error_is_mapped_to_backend_unreachable():
    service, agent = make_service()

    def boom(path: str):
        raise OSError("connection refused")

    service._supabase_get = boom
    service.handle("req-17", "search_symbols", {"query": "NV"})
    payload = agent.transport.sent[-1].payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "backend_unreachable"

# ── pane scales (viewer LOG/LIN buttons) ─────────────────────────────────


def test_set_pane_scales_uses_the_merge_endpoint(monkeypatch):
    service, agent = make_service()
    calls = []

    def fake_pca(method, path, payload=None):
        calls.append((method, path, payload))
        return {"status": "success", "pane_scales": payload["pane_scales"]}

    monkeypatch.setattr(service, "_pca_json", fake_pca)

    result = service.set_pane_scales("qmaggi", {"MAIN": "log", "volume": "bogus"})

    assert calls == [
        ("PATCH", "/api/presets/qmaggi/pane_scales", {"pane_scales": {"main": "log", "volume": "linear"}})
    ]
    assert result["pane_scales"] == {"main": "log", "volume": "linear"}


def test_set_pane_scales_quotes_the_preset_id(monkeypatch):
    service, agent = make_service()
    calls = []
    monkeypatch.setattr(service, "_pca_json", lambda method, path, payload=None: calls.append(path) or {})

    service.set_pane_scales("my preset", {"main": "log"})

    assert calls == ["/api/presets/my%20preset/pane_scales"]


def test_set_pane_scales_requires_a_preset_id():
    service, agent = make_service()
    with pytest.raises(ControlError) as err:
        service.set_pane_scales("  ", {"main": "log"})
    assert err.value.code == "invalid_request"

