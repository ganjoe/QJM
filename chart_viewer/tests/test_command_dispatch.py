"""Tests fuer den extrahierten /api/command-Dispatch (command_api.handle_command).

Phase 0/1 des Plans CHARTVIEWER_CHART_BUILDER_API_PLAN.md: der Dispatch ist aus
run_server.ControlHandler.do_POST herausgezogen und damit ohne Socket testbar.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from chart_viewer.agent.command_api import handle_command


class FakeTransport:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    def send_command(self, envelope) -> None:
        self.sent.append(envelope)


class FakeControlService:
    """Zeichnet die Delegation auf und liefert ein vorgegebenes Ergebnis."""

    def __init__(self, result: Dict[str, Any] | None = None) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.result = result or {"ok": True, "data": {"fenster": "ok"}}

    def execute(self, op: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((op, params))
        return self.result


class FakeAgent:
    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.layout_ledger: Dict[str, Any] = {}
        self.series_data: Dict[str, Any] = {}
        self.control_service = FakeControlService()
        self.opened: List[Dict[str, Any]] = []
        self.snapshots: List[Tuple[str, Dict[str, Any]]] = []

    def open_window(self, **kwargs) -> None:
        self.opened.append(kwargs)

    def send_snapshot(self, window_id: str, payload: Dict[str, Any]) -> None:
        self.snapshots.append((window_id, payload))


def test_bridge_delegates_lifecycle_actions_to_the_control_service():
    for action, op in (
        ("GET_CHART_STATE", "get_chart_state"),
        ("LIST_WINDOWS", "list_windows"),
        ("SAVE_CHART", "save_chart"),
        ("APPLY_CHART", "apply_chart"),
    ):
        agent = FakeAgent()
        result = handle_command(
            {"action": action, "window_id": "win_1", "chart_id": "c1"},
            agent,
            agent.transport,
        )
        assert result == agent.control_service.result
        assert agent.control_service.calls == [(op, {"window_id": "win_1", "chart_id": "c1"})]


def test_bridge_accepts_lowercase_action_names():
    agent = FakeAgent()
    handle_command({"action": "list_windows"}, agent, agent.transport)
    assert agent.control_service.calls == [("list_windows", {})]


def test_bridge_reports_a_missing_control_service():
    agent = FakeAgent()
    agent.control_service = None
    result = handle_command({"action": "LIST_WINDOWS"}, agent, agent.transport)
    assert result["ok"] is False and result["error"]["code"] == "internal"


def test_bridge_passes_service_errors_through_unchanged():
    agent = FakeAgent()
    agent.control_service = FakeControlService(
        {"ok": False, "error": {"code": "already_exists", "message": "gibt es schon"}}
    )
    result = handle_command({"action": "SAVE_CHART", "window_id": "w"}, agent, agent.transport)
    assert result["ok"] is False and result["error"]["code"] == "already_exists"


def test_unknown_action_is_still_reported():
    agent = FakeAgent()
    result = handle_command({"action": "NOT_A_THING"}, agent, agent.transport)
    assert "Unknown action" in result["error"]


def test_open_window_still_works_after_the_extraction():
    agent = FakeAgent()
    result = handle_command(
        {"action": "OPEN_WINDOW", "symbol": "NVDA", "timeframe": {"unit": "D", "multiplier": 1}},
        agent,
        agent.transport,
    )
    assert result["status"] == "ok"
    assert result["window_id"] == "win_nvda_1d"
    assert agent.opened and agent.opened[0]["symbol"] == "NVDA"


def test_close_window_removes_the_ledger_entry_and_commands_the_viewer():
    agent = FakeAgent()
    agent.layout_ledger["win_1"] = {"symbol": "NVDA"}
    result = handle_command({"action": "CLOSE_WINDOW", "window_id": "win_1"}, agent, agent.transport)
    assert result["status"] == "ok"
    assert "win_1" not in agent.layout_ledger
    assert len(agent.transport.sent) == 1
