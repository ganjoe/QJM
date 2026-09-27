"""Tests for the watchlist dialogs used by the control panel."""

from PySide6.QtWidgets import QDialog

from chart_viewer.ui import watchlist_dialogs
from chart_viewer.ui.watchlist_dialogs import (
    NewWatchlistDialog,
    clean_list_name,
    is_reserved_name,
)


def test_clean_list_name_mirrors_the_agent_sanitizer():
    assert clean_list_name(" 10_favorite ") == "10_favorite"
    assert clean_list_name("scan/latest;drop") == "scanlatestdrop"
    assert clean_list_name("a" * 80) == "a" * 64


def test_reserved_names_are_the_master_universe_aliases():
    assert is_reserved_name("ALL")
    assert is_reserved_name("all.txt")
    assert not is_reserved_name("all_positions")


def test_new_dialog_rejects_missing_reserved_and_duplicate_names(qapp):
    dialog = NewWatchlistDialog(existing=["10_favorite"], sources=["10_favorite"])
    assert dialog.source_combo.currentData() == ""

    dialog.name_edit.setText("   ")
    dialog._on_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "Namen" in dialog.hint.text()

    dialog.name_edit.setText("all")
    dialog._on_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "reserviert" in dialog.hint.text()

    dialog.name_edit.setText("10_Favorite")
    dialog._on_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "existiert bereits" in dialog.hint.text()


def test_new_dialog_returns_cleaned_name_and_selected_source(qapp):
    dialog = NewWatchlistDialog(existing=["10_favorite"], sources=["10_favorite", "scan_latest"])
    dialog.name_edit.setText(" 30_copy!! ")
    dialog.source_combo.setCurrentIndex(dialog.source_combo.findData("10_favorite"))
    dialog._on_accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.values() == ("30_copy", "10_favorite")


def test_ask_watchlist_without_options_returns_none(qapp):
    """No lists -> no modal dialog, just None (used by copy/move/delete)."""
    assert watchlist_dialogs.ask_watchlist(None, "Kopieren", "Ziel:", []) is None
