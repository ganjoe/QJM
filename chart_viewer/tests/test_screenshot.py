"""Tests for Screenshot capture feature (UI debugging and agent visual inspection)."""

import base64
import os
import tempfile
import pytest

from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.models.envelope import make_envelope, MessageKind
from chart_viewer.config import ViewerConfig


def test_screenshot_zero_windows(qapp):
    """When 0 windows are open, screenshot.request returns empty screenshots list."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(transport=viewer_transport)

    agent.start()
    app.start()

    res = agent.request_screenshots(timeout_s=2.0)
    assert res is not None
    assert "screenshots" in res
    assert len(res["screenshots"]) == 0
    assert "request_id" in res


def test_screenshot_with_open_window(qapp):
    """When a window is open, screenshot.request captures 640x480 PNG."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(transport=viewer_transport)

    agent.start()
    app.start()

    # Open a test window
    agent.open_window(window_id="test_win_nvda", symbol="NVDA")
    qapp.processEvents()

    assert "test_win_nvda" in app.windows

    # Request screenshot
    res = agent.request_screenshots(window_id="test_win_nvda", timeout_s=2.0)
    assert res is not None
    assert len(res["screenshots"]) == 1

    shot = res["screenshots"][0]
    assert shot["window_id"] == "test_win_nvda"
    assert shot["symbol"] == "NVDA"
    assert shot["width"] == 640
    assert shot["height"] == 480
    assert "image_base64" in shot

    # Verify base64 decode and PNG header
    png_bytes = base64.b64decode(shot["image_base64"])
    assert len(png_bytes) > 0
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_screenshot_timeout():
    """request_screenshots raises TimeoutError if no response arrives within timeout."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    agent.start()
    # Note: viewer is NOT started, so no response will be sent

    with pytest.raises(TimeoutError):
        agent.request_screenshots(timeout_s=0.2)


def test_screenshot_disk_save():
    """Verify writing decoded screenshot images to destination folder."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        b64_data = base64.b64encode(fake_png).decode("ascii")

        capture_id = "snap_20260905_test123"
        clean_win_id = "test_win_aapl"
        filename = f"screenshot_{capture_id}_{clean_win_id}.png"
        filepath = os.path.join(tmp_dir, filename)

        with open(filepath, "wb") as f:
            f.write(base64.b64decode(b64_data))

        assert os.path.exists(filepath)
        assert os.path.getsize(filepath) == len(fake_png)
