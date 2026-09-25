import logging
from types import SimpleNamespace

from core import diagnostics as diag


def test_recoverable_logs_are_rate_limited_and_do_not_expose_payload(monkeypatch, caplog):
    now = [0.0]
    monkeypatch.setattr(diag, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(diag, "_last", {})
    log = logging.getLogger("UmbraNet.test.recoverable")
    caplog.set_level(logging.DEBUG, logger=log.name)
    error = ValueError("https://user:password@example.org/?token=SECRET")
    for _ in range(1000):
        diag.log_recoverable(log, "DNS fallback", error)
    assert len(caplog.records) == 1
    assert "ValueError" in caplog.text
    assert "SECRET" not in caplog.text and "password" not in caplog.text
    now[0] = 60
    diag.log_recoverable(log, "DNS fallback", error)
    assert len(caplog.records) == 2
    diag.log_recoverable(log, "Different operation", error, level=logging.WARNING)
    assert caplog.records[-1].levelno == logging.WARNING


def test_disabled_debug_does_not_consume_rate_limit(monkeypatch, caplog):
    monkeypatch.setattr(diag, "_last", {})
    log = logging.getLogger("UmbraNet.test.disabled")
    caplog.set_level(logging.INFO, logger=log.name)
    diag.log_recoverable(log, "optional operation", OSError())
    assert not diag._last
    caplog.set_level(logging.DEBUG, logger=log.name)
    diag.log_recoverable(log, "optional operation", OSError())
    assert len(caplog.records) == 1
