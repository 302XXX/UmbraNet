"""Notification-only UI: opening a release is always an explicit user action."""
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
from PySide6.QtWidgets import QApplication

from core.update_checker import UpdateChecker, UpdateResult
from umbranet.views import about

APP = QApplication.instance() or QApplication([])


@pytest.fixture
def view(monkeypatch):
    checker = UpdateChecker("0.4.0-dev")
    monkeypatch.setattr(about.ea, "get_update_checker", lambda: checker)
    monkeypatch.setattr(about.ea, "set_update_channel", checker.set_channel)
    monkeypatch.setattr(about.ea, "get_startup_health", lambda: {"summary": "OK", "severity": "ok"})
    monkeypatch.setattr(checker, "check_async", Mock(return_value=True))
    widget = about.AboutView()
    yield widget, checker
    widget._update_poll.stop()
    widget.close()
    widget.deleteLater()


def test_release_is_not_opened_automatically(view, monkeypatch):
    widget, checker = view
    opened = Mock(return_value=True)
    monkeypatch.setattr(about.QDesktopServices, "openUrl", opened)
    checker._result = UpdateResult("available", "0.4.0",
        "https://github.com/302XXX/UmbraNet/releases/tag/v0.4.0", "Доступна версия 0.4.0")
    widget._refresh_update_status()
    assert not widget._open_release.isHidden()
    assert "0.4.0" in widget._release_status.text()
    opened.assert_not_called()
    widget._open_release.click()
    assert opened.call_args.args[0].toString() == checker.result.url


def test_error_and_no_release_have_no_download_button(view):
    widget, checker = view
    for state in ("error", "no_releases", "current"):
        checker._result = UpdateResult(state, message=state)
        widget._refresh_update_status()
        assert widget._open_release.isHidden()
        assert widget._check_release.isEnabled()


def test_manual_check_and_prerelease_opt_in(view):
    widget, checker = view
    assert not widget._prereleases.isChecked()
    widget._check_release.click()
    checker.check_async.assert_called_once_with(force=True)
    widget._prereleases.setChecked(True)
    assert checker.include_prereleases
    assert checker.check_async.call_count == 2
