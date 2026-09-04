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
