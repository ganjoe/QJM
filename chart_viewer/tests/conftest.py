from chart_viewer.config import ViewerConfig
"""Pytest configuration and QApplication fixture for headless test execution."""

import os
import pytest

# Ensure headless offscreen rendering for Qt tests
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _control_panel_default_off(request, monkeypatch):
    """Run the legacy Qt tests without the control panel.

    The panel is a top-level window with its own timers; dozens of leaked panel
    instances across unrelated test modules made Qt event processing flaky. The
    panel has dedicated tests (tests/test_control_panel.py) that enable it
    explicitly and tear it down deterministically.
    """
    if "test_control_panel.py" in str(request.fspath):
        return
    monkeypatch.setattr(ViewerConfig, "control_panel_enabled", False, raising=False)
