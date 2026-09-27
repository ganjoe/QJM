"""Setup-Slot-Binding (Plan Phase 6).

Ein Agent soll einem gespeicherten Setup-Slot ein Chart/Symbol/Timeframe
zuweisen koennen, ohne Fenster zu oeffnen oder zu schliessen.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from chart_viewer.agent import agent_client
from chart_viewer.agent.command_api import handle_command


class FakeTransport:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    def send_command(self, envelope) -> None:
        self.sent.append(envelope)


class FakeAgent:
    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.layout_ledger: Dict[str, Any] = {}
        self.series_data: Dict[str, Any] = {}
        self.slots: List[Dict[str, Any]] = []

    def assign_setup_slot(self, setup_name, **kwargs):
        self.slots.append({"setup_name": setup_name, **kwargs})
        return {"status": "ok", "setup_name": setup_name, "slot": kwargs.get("slot") or 1,
                "changed": kwargs}

    def save_setup(self, setup_name, geometries=None, monitor_infos=None, force=False):
        return {"status": "ok", "setup_name": setup_name, "monitor_count": 2, "window_count": 3}


def test_setup_assign_dispatches_to_the_agent():
    agent = FakeAgent()
    result = handle_command(
        {"action": "SETUP_ASSIGN", "setup_name": "desk", "slot": 2,
         "chart_id": "momentum", "symbol": "nvda"},
        agent,
        agent.transport,
    )
    assert result["status"] == "ok"
    assert agent.slots == [{
        "setup_name": "desk", "monitor_count": None, "window_id": None, "slot": 2,
        "chart_id": "momentum", "symbol": "nvda", "timeframe": None,
    }]


def test_setup_assign_requires_a_setup_name():
    agent = FakeAgent()
    result = handle_command({"action": "SETUP_ASSIGN", "slot": 1}, agent, agent.transport)
    assert result == {"error": "Missing 'setup_name' parameter"}


def test_save_setup_pins_slots_after_saving():
    agent = FakeAgent()
    result = handle_command(
        {
            "action": "SAVE_SETUP",
            "setup_name": "desk",
            "windows": [
                {"window_id": "win_a", "chart_id": "momentum"},
                {"slot": 2, "symbol": "amd"},
            ],
        },
        agent,
        agent.transport,
    )
    assert result["window_count"] == 3 and result["monitor_count"] == 2
    assert [pin["changed"]["window_id"] for pin in result["pinned"]] == ["win_a", None]
    assert agent.slots[0]["chart_id"] == "momentum"
    assert agent.slots[1]["slot"] == 2 and agent.slots[1]["symbol"] == "amd"


def test_assign_setup_slot_rewrites_only_the_target_window(monkeypatch):
    rows = [{
        "id": 7, "setup_name": "desk", "monitor_count": 2,
        "windows": [
            {"window_id": "win_a", "chart_id": "chart_a", "symbol": "NVDA", "timeframe": "1D",
             "chart_panes": [{"pane_id": "main"}]},
            {"window_id": "win_b", "chart_id": "chart_b", "symbol": "AMD", "timeframe": "1D"},
        ],
    }]
    monkeypatch.setattr(agent_client, "_supabase_get", lambda path: rows)
    patched: List[Any] = []
    monkeypatch.setattr(
        agent_client, "_supabase_patch", lambda path, payload: patched.append((path, payload)) or {}
    )

    agent = object.__new__(agent_client.ChartAgent)
    agent.screens = []
    result = agent.assign_setup_slot("desk", monitor_count=2, window_id="win_a", chart_id="chart_neu")

    assert result["slot"] == 1 and result["changed"] == {"chart_id": "chart_neu"}
    path, payload = patched[0]
    assert path == "/pca_window_setups?id=eq.7"
    first, second = payload["windows"]
    assert first["chart_id"] == "chart_neu" and "chart_panes" not in first
    assert first["symbol"] == "NVDA" and first["timeframe"] == "1D"
    assert second == {"window_id": "win_b", "chart_id": "chart_b", "symbol": "AMD", "timeframe": "1D"}
    # Das Original bleibt unberuehrt (Kopie, kein In-Place-Schreiben).
    assert rows[0]["windows"][0]["chart_id"] == "chart_a"


def test_assign_setup_slot_by_slot_number_and_unknown_targets(monkeypatch):
    rows = [{"id": 1, "setup_name": "desk", "monitor_count": 1,
             "windows": [{"window_id": "win_a"}, {"window_id": "win_b"}]}]
    monkeypatch.setattr(agent_client, "_supabase_get", lambda path: rows)
    monkeypatch.setattr(agent_client, "_supabase_patch", lambda path, payload: {})

    agent = object.__new__(agent_client.ChartAgent)
    agent.screens = []
    result = agent.assign_setup_slot("desk", slot=2, symbol="tsla", timeframe="1h")
    assert result["slot"] == 2 and result["window_id"] == "win_b"
    assert result["changed"] == {"symbol": "TSLA", "timeframe": "1h"}

    with pytest.raises(RuntimeError):
        agent.assign_setup_slot("desk", slot=9, chart_id="x")
    with pytest.raises(RuntimeError):
        agent.assign_setup_slot("desk", window_id="gibt_es_nicht", chart_id="x")


def test_list_setups_exposes_the_chart_bindings(monkeypatch):
    """Der DELETE_CHART-Schutz braucht die chart_id je Fenster in LIST_SETUPS."""
    rows = [
        {
            "id": 1, "setup_name": "desk", "monitor_count": 2,
            "updated_at": "2026-09-28T10:00:00Z",
            "windows": [
                {"window_id": "win_a", "chart_id": "momentum", "symbol": "NVDA", "timeframe": "1D"},
                {"window_id": "win_b", "chart_id": "default", "symbol": "AMD", "timeframe": "1h"},
            ],
        },
    ]
    monkeypatch.setattr(agent_client, "_supabase_get", lambda path: rows)

    agent = object.__new__(agent_client.ChartAgent)
    setups = agent.list_setups()

    assert [s["setup_name"] for s in setups] == ["desk"]
    variant = setups[0]["variants"][0]
    assert variant["window_count"] == 2
    assert variant["windows"] == [
        {"window_id": "win_a", "chart_id": "momentum", "symbol": "NVDA", "timeframe": "1D"},
        {"window_id": "win_b", "chart_id": "default", "symbol": "AMD", "timeframe": "1h"},
    ]


def test_assign_setup_slot_clears_the_chart_binding(monkeypatch):
    rows = [{"id": 3, "setup_name": "desk", "monitor_count": 1,
             "windows": [{"window_id": "win_a", "chart_id": "alt", "chart_name": "Alt",
                          "chart_panes": [{"pane_id": "main"}]}]}]
    monkeypatch.setattr(agent_client, "_supabase_get", lambda path: rows)
    monkeypatch.setattr(agent_client, "_supabase_patch", lambda path, payload: {})

    agent = object.__new__(agent_client.ChartAgent)
    agent.screens = []
    result = agent.assign_setup_slot("desk", slot=1, chart_id="")

    assert result["changed"] == {"chart_id": None}
