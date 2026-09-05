from chart_viewer.config import ViewerConfig
"""End-to-End Test for Screenshot Capture and Disk Saving."""

import base64
import os
import struct
import tempfile
import pytest

from chart_viewer.ui.app import ViewerApp
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.transport.in_process import create_in_process_pair


def get_png_dimensions(data: bytes) -> tuple[int, int]:
    """Parse PNG IHDR chunk to extract width and height."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "Not a valid PNG"
    # IHDR chunk is immediately after the 8-byte PNG signature:
    # 4 bytes length, 4 bytes chunk type ("IHDR"), 4 bytes width, 4 bytes height
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_screenshot_e2e_multiple_windows_and_resolution(qapp):
    """Test capturing screenshots of multiple open windows and verifying 640x480 resolution."""
    viewer_transport, agent_transport = create_in_process_pair()
    agent = ChartAgent(transport=agent_transport)
    app = ViewerApp(config=ViewerConfig(), transport=viewer_transport)

    agent.start()
    app.start()

    # Open 2 chart windows
    agent.open_window(window_id="win_nvda_1d", symbol="NVDA")
    agent.open_window(window_id="win_aapl_1d", symbol="AAPL")
    qapp.processEvents()

    assert "win_nvda_1d" in app.windows
    assert "win_aapl_1d" in app.windows

    # Execute screenshot request
    res = agent.request_screenshots(timeout_s=3.0)
    assert res is not None
    assert "screenshots" in res
    assert len(res["screenshots"]) == 2

    # Verify each screenshot
    with tempfile.TemporaryDirectory() as tmp_dir:
        capture_id = res.get("request_id")
        assert capture_id.startswith("snap_")

        for shot in res["screenshots"]:
            w_id = shot["window_id"]
            symbol = shot["symbol"]
            assert symbol in ("NVDA", "AAPL")
            assert shot["width"] == 640
            assert shot["height"] == 480

            raw_bytes = base64.b64decode(shot["image_base64"])
            w, h = get_png_dimensions(raw_bytes)
            assert w == 640, f"Expected width 640, got {w}"
            assert h == 480, f"Expected height 480, got {h}"

            # Verify saving to disk
            filename = f"screenshot_{capture_id}_{w_id}.png"
            filepath = os.path.join(tmp_dir, filename)
            with open(filepath, "wb") as f:
                f.write(raw_bytes)

            assert os.path.isfile(filepath)
            assert os.path.getsize(filepath) > 500  # Non-empty valid image
