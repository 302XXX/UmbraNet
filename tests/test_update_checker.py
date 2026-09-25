import io
import json
import threading
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import update_checker as uc


def release(tag, **kwargs):
    return {"tag_name": tag, "draft": False, "prerelease": False, **kwargs}


@pytest.mark.parametrize("current,tag,state", [
    ("0.4.0-dev", "v0.4.0", "available"),
    ("0.4.0rc1", "v0.4.0", "available"),
    ("0.4.0", "v0.4.0", "current"),
    ("0.4.1", "v0.4.0", "current"),
    ("0.9.0", "v0.10.0", "available"),
])
def test_version_comparison(current, tag, state):
    result = uc.select_release([release(tag)], current)
    assert result.state == state


def test_stable_channel_filters_drafts_prereleases_and_bad_tags():
    releases = [release("v0.5.0rc1"), release("v0.6.0", prerelease=True),
                release("v0.7.0", draft=True), release("nonsense"), None,
                release("v0.4.0"), release("v99.0+local")]
    result = uc.select_release(releases, "0.3.0")
    assert result.version == "0.4.0"
    assert uc.select_release(releases, "0.3.0", True).version == "0.6.0"


def test_empty_repository_and_no_eligible_releases():
    assert uc.select_release([], "0.4.0-dev").state == "no_releases"
    assert uc.select_release([release("v0.4.0rc1")], "0.4.0-dev").state == "no_releases"


def test_sort_by_version_and_ignore_remote_html_url():
    result = uc.select_release([
        release("v0.10.0", html_url="file:///malicious"), release("v0.9.0")
    ], "0.4.0")
    assert result.url == uc.RELEASES_PAGE + "/tag/v0.10.0"


@pytest.mark.parametrize("code,empty", [(404, True), (403, False), (429, False), (500, False)])
def test_http_statuses(monkeypatch, code, empty):
    monkeypatch.setattr(uc.urllib.request, "urlopen", Mock(side_effect=
        urllib.error.HTTPError(uc.RELEASES_API, code, "test", {}, None)))
    if empty:
        assert uc.fetch_releases(False) == []
    else:
        with pytest.raises(urllib.error.HTTPError):
            uc.fetch_releases(False)


@pytest.mark.parametrize("channel,payload", [(False, release("v0.4.0")), (True, [release("v0.5.0rc1")])])
def test_request_and_response(monkeypatch, channel, payload):
    opened = Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(uc.urllib.request, "urlopen", opened)
    rows = uc.fetch_releases(channel)
    assert len(rows) == 1
    request = opened.call_args.args[0]
    assert request.full_url.endswith("?per_page=100" if channel else "/latest")
    assert opened.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("raw", [
    pytest.param(b"[]", id="array-not-object"),
    pytest.param(b"{}", id="missing-tag"),
    pytest.param(b"<html>error</html>", id="invalid-json"),
    pytest.param(b"x" * (uc.MAX_RESPONSE + 1), id="over-2MiB"),
])
def test_invalid_response_is_not_reported_as_up_to_date(monkeypatch, raw):
    monkeypatch.setattr(uc.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(raw))
    with pytest.raises(ValueError):
        uc.fetch_releases(False)


class InlineThread:
    """Deterministic test double; concurrency is covered with real Events below."""
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


def test_periodic_check_throttle_and_retry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(uc, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(uc, "threading", SimpleNamespace(Lock=threading.Lock, Thread=InlineThread))
    fetch = Mock(side_effect=[TimeoutError, [release("v0.4.0")]])
    monkeypatch.setattr(uc, "fetch_releases", fetch)
    checker = uc.UpdateChecker("0.4.0-dev")
    assert checker.check_async()
    assert checker.result.state == "error"
    assert not checker.check_async()
    now[0] += uc.RETRY_INTERVAL
    assert checker.check_async()
    assert checker.result.state == "available"
    now[0] += uc.CHECK_INTERVAL - 1
    assert not checker.check_async()


def test_inflight_channel_change_discards_result_and_duplicate_check(monkeypatch):
    entered, release_worker = threading.Event(), threading.Event()
    def fetch(channel):
        entered.set()
        assert release_worker.wait(3)
        return [release("v0.5.0rc1")]
    monkeypatch.setattr(uc, "fetch_releases", fetch)
    real_thread = threading.Thread
    threads = []
    def make_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(uc, "threading", SimpleNamespace(Lock=threading.Lock, Thread=make_thread))
    checker = uc.UpdateChecker("0.4.0", include_prereleases=True)
    assert checker.check_async()
    assert entered.wait(2)
    assert not checker.check_async(force=True)
    checker.set_channel(False)
    release_worker.set()
    threads[0].join(2)
    assert checker.result.state == "idle"
    assert not checker.busy
    assert checker.check_async()
    threads[1].join(2)
    assert checker.result.state == "no_releases"


def test_thread_start_failure_is_recoverable(monkeypatch):
    thread = Mock()
    thread.start.side_effect = RuntimeError("no threads")
    monkeypatch.setattr(uc, "threading", SimpleNamespace(Lock=threading.Lock, Thread=lambda **k: thread))
    checker = uc.UpdateChecker("0.4.0")
    assert not checker.check_async()
    assert not checker.busy
    assert checker.result.state == "error"
