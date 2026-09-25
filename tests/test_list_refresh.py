import io
import json
import threading
import urllib.request
from unittest.mock import Mock

import pytest

import dns_server as dns
from core.dpi import domain_updater as du


@pytest.fixture
def engine(tmp_path, monkeypatch):
    # No process trackers, GUI, or background networking from __init__.
    eng = dns.UmbraNetDNS.__new__(dns.UmbraNetDNS)
    eng.config = {"routed_subscriptions": ["https://example.org/list?token=SECRET"],
                  "subscribed_domains_set": {"old.example"}}
    eng._subscriptions_lock = threading.Lock()
    monkeypatch.setattr(dns, "_CORE_DIR", str(tmp_path))
    (tmp_path / "subscribed_domains_cache.json").write_text('["old.example"]')
    return eng


def response(raw):
    result = io.BytesIO(raw)
    result.headers = {}
    return result


def test_real_subscription_refresh_updates_config_and_atomic_cache(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k:
                        response(b"# comment\n0.0.0.0 new.example\nother.example\n"))
    assert engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"new.example", "other.example"}
    path = tmp_path / "subscribed_domains_cache.json"
    assert set(json.loads(path.read_text())) == {"new.example", "other.example"}
    assert json.loads(path.with_suffix(".json.bak").read_text()) == ["old.example"]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("body", [b"", b"<html>error.example</html>", b"# temporarily unavailable"])
def test_empty_or_invalid_subscription_preserves_cache(engine, tmp_path, monkeypatch, body):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(body))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert json.loads((tmp_path / "subscribed_domains_cache.json").read_text()) == ["old.example"]


def test_subscription_failure_preserves_cache_without_logging_tokens(engine, monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=OSError("url?token=SECRET")))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert "SECRET" not in caplog.text


def test_manual_and_scheduled_subscription_refresh_do_not_overlap(engine, monkeypatch):
    entered, release, done, duplicate = (threading.Event() for _ in range(4))
    calls = []
    def fetch():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return True
    monkeypatch.setattr(engine, "_fetch_subscriptions", fetch)
    results = []
    engine.update_subscriptions_async(lambda *r: (results.append(r), done.set()))
    assert entered.wait(2)
    assert not engine._update_subscriptions()
    engine.update_subscriptions_async(lambda *r: (results.append(r), duplicate.set()))
    assert duplicate.wait(2)
    assert results == [(False, 1)]
    release.set()
    assert done.wait(2)
    assert results[-1] == (True, 1)
    assert len(calls) == 1
    assert engine._subscriptions_lock.acquire(False)
    engine._subscriptions_lock.release()


def test_changed_subscriptions_discard_old_download(engine, tmp_path, monkeypatch):
    def fetch(*args, **kwargs):
        engine.config["routed_subscriptions"] = ["https://example.org/new-list"]
        return response(b"stale.example")
    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}
    assert json.loads((tmp_path / "subscribed_domains_cache.json").read_text()) == ["old.example"]


def test_all_subscription_sources_must_succeed(engine, tmp_path, monkeypatch):
    engine.config["routed_subscriptions"].append("https://example.org/other")
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=[response(b"new.example"), TimeoutError]))
    assert not engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"old.example"}


def test_explicit_subscription_removal_clears_cache(engine, tmp_path):
    engine.config["routed_subscriptions"] = []
    assert engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == set()
    assert not (tmp_path / "subscribed_domains_cache.json").exists()


def strategy(tmp_path, **extra):
    data = {"id": "test", "remote_url": "https://example.org/domains?SECRET", "args": ["--original"]}
    data.update(extra)
    path = tmp_path / "test.json"
    path.write_text(json.dumps(data))
    return path


class StreamingResponse:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        pass
    def iter_content(self, chunk_size):
        yield self.data


def test_domain_cache_refresh_never_changes_strategy(tmp_path, monkeypatch):
    path = strategy(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(du.requests, "get", lambda *a, **k: StreamingResponse(b"example.org\nExample.NET\n"))
    assert du.update_all_strategies(tmp_path)
    assert path.read_bytes() == before
    assert (tmp_path / "remote_hostlist_test.txt").read_text() == "example.net\nexample.org\n"
    assert not list(tmp_path.glob("*.tmp"))


# Explicit IDs are essential: pytest otherwise puts the entire 5 MiB body in
# the node ID and streams it as one enormous line in CI's verbose output.
@pytest.mark.parametrize("raw", [
    pytest.param(b"", id="empty"),
    pytest.param(b"<html>example.org</html>", id="html"),
    pytest.param(b"--dpi-desync=fake", id="winws-option"),
    pytest.param(b"x" * (du.MAX_BYTES + 1), id="over-5MiB"),
])
def test_bad_remote_hostlist_preserves_working_file(tmp_path, monkeypatch, raw):
    strategy(tmp_path)
    cache = tmp_path / "remote_hostlist_test.txt"
    cache.write_text("old.example\n")
    monkeypatch.setattr(du.requests, "get", lambda *a, **k: StreamingResponse(raw))
    assert not du.update_all_strategies(tmp_path)
    assert cache.read_text() == "old.example\n"


def test_strategy_id_cannot_escape_cache_directory(tmp_path, monkeypatch):
    strategy(tmp_path, id="../../escape")
    fetch = Mock()
    monkeypatch.setattr(du.requests, "get", fetch)
    assert not du.update_all_strategies(tmp_path)
    fetch.assert_not_called()


def test_current_uz_strategy_has_no_remote_fetch(tmp_path, monkeypatch):
    strategy(tmp_path, remote_url=None)
    fetch = Mock()
    monkeypatch.setattr(du.requests, "get", fetch)
    assert du.update_all_strategies(tmp_path)
    fetch.assert_not_called()


def test_hostlist_updates_guard_duplicate_requests(tmp_path):
    with du._lock:
        assert not du.update_all_strategies(tmp_path)


def test_subscription_url_lines_are_not_mistaken_for_comments(engine, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k:
                        response(b"// comment\nhttps://example.org/path // comment\n"))
    assert engine._update_subscriptions()
    assert engine.config["subscribed_domains_set"] == {"example.org"}
