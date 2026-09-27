"""Topbar-Vorrang (Plan Phase 5).

Regel: chart.topbar_metrics bestimmt den Metrik-Streifen; SET_TOPBAR-Bloecke sind
fensterlokal und werden von einem Re-Render (COMPOSE/APPLY/DISPLAY) NICHT
geloescht. Wer den Streifen aendern will, aendert das Chart.
"""

from __future__ import annotations

from typing import Any, Dict, List

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


def test_set_topbar_persists_a_window_local_block():
    agent = FakeAgent()
    agent.layout_ledger["win_1"] = {"symbol": "NVDA"}
    result = handle_command(
        {
            "action": "SET_TOPBAR",
            "window_id": "win_1",
            "block_id": "status_block",
            "content": "RS 92",
        },
        agent,
        agent.transport,
    )
    assert result["status"] == "ok"
    blocks = agent.series_data["win_1"]["topbar_blocks"]
    assert blocks == [
        {
            "block_id": "status_block",
            "position": {"row": 0, "col": 0},
            "content": "RS 92",
            "ttl_ms": None,
        }
    ]


def test_compose_chart_does_not_clear_topbar_blocks(monkeypatch):
    from chart_viewer.agent.control_service import ControlService, ControlConfig

    agent = FakeAgent()
    agent.layout_ledger["win_1"] = {
        "symbol": "NVDA",
        "timeframe": {"unit": "D", "multiplier": 1},
        "chart": {
            "id": "mein_chart",
            "display_name": "Mein Chart",
            "draft": False,
            "topbar_metrics": ["ibd_rs"],
            "panes": [{"pane_id": "main", "pane_preset_id": "candles", "scale": "linear", "weight": 7}],
        },
    }
    agent.series_data["win_1"] = {"topbar_blocks": [{"block_id": "status_block", "content": "RS 92"}]}

    service = ControlService(
        agent,
        config=ControlConfig(
            pca_url="http://pca.test:8791",
            control_api_url="http://control.test:8766",
            stock_data_url="http://stock.test:8002",
            http_timeout_s=1.0,
            search_cache_ttl_s=30.0,
        ),
    )
    posted: List[dict] = []
    monkeypatch.setattr(service, "_http_json", lambda method, url, payload=None, **kw: posted.append(payload) or {})

    service.execute(
        "compose_chart",
        {"window_id": "win_1", "panes": [{"pane_preset_id": "builtin:candles"}, {"pane_preset_id": "builtin:volume"}]},
    )

    assert agent.series_data["win_1"]["topbar_blocks"] == [{"block_id": "status_block", "content": "RS 92"}]
    # Der Metrik-Streifen kommt aus dem Chart, nicht aus dem Fenster.
    assert posted[0]["chart_meta"]["topbar_metrics"] == ["ibd_rs"]
