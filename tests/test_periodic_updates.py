"""Real scheduler/cache paths; fake clock and network, no 24h sleeps."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import bogus_updater as bu


def clock(monkeypatch):
    ticks = [100.0]
    monkeypatch.setattr(bu, "time", SimpleNamespace(
        monotonic=lambda: ticks[0], time=lambda: 1_000_000 + ticks[0]))
    return ticks


def test_independent_deadlines_retry_and_24_hours(tmp_path, monkeypatch):
    now = clock(monkeypatch)
    updater = bu.BogusUpdater(str(tmp_path))
    bogus = Mock(side_effect=[False, True, True])
    subs = Mock(return_value=True)
    domains = Mock(side_effect=[OSError("offline"), True, True])
    updater._tasks[0].callback = bogus
    updater.add_task("subscriptions", subs)
    updater.add_task("domains", domains)
    updater._run_due()
    assert [t.next_due for t in updater._tasks] == [3700, 86500, 3700]
    now[0] += 60
    updater._run_due()
    assert bogus.call_count == subs.call_count == domains.call_count == 1
    now[0] = 3700
    updater._run_due()
    assert bogus.call_count == domains.call_count == 2
    assert subs.call_count == 1
    now[0] = 86500
    updater._run_due()
    assert subs.call_count == 2
    assert bogus.call_count == domains.call_count == 2


def test_stop_skips_remaining_jobs(tmp_path):
    updater = bu.BogusUpdater(str(tmp_path))
    updater._tasks[0].callback = lambda: updater._stop_event.set()
    other = Mock(return_value=True)
    updater.add_task("other", other)
    updater._run_due()
    other.assert_not_called()


def test_start_idempotent_and_stops(tmp_path):
    updater = bu.BogusUpdater(str(tmp_path))
    called = threading.Event()
    updater._tasks[0].callback = lambda: called.set() or True
    updater.start()
    first = updater._thread
    assert called.wait(2)
    updater.start()
    assert updater._thread is first
    with pytest.raises(RuntimeError):
        updater.add_task("too-late", lambda: True)
    updater.stop()
    assert not first.is_alive()
    updater.start()
    assert updater._thread is not first
    updater.stop()


def test_restart_requested_while_previous_worker_is_stopping(tmp_path):
    updater = bu.BogusUpdater(str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    def callback():
        entered.set()
        assert release.wait(5)
        return True
    updater._tasks[0].callback = callback
    updater.start()
    old = updater._thread
    assert entered.wait(2)
    # Same state as stop() after its bounded join times out.
    updater._stop_event.set()
    updater.start()
    assert updater._thread is old
    release.set()
    old.join(2)
    assert not old.is_alive()
    assert updater._thread is not None and updater._thread is not old
    updater.stop()


def test_offline_preserves_cache_and_retries_in_hour(tmp_path, monkeypatch):
    now = clock(monkeypatch)
    bu._save_cache(str(tmp_path), ["10.0.0.99"], [])
    cache = tmp_path / bu.CACHE_FILENAME
    before = cache.read_bytes()
    monkeypatch.setattr(bu, "_fetch_remote", Mock(side_effect=TimeoutError))
    monkeypatch.setattr(bu, "_load_from_local", lambda: (["1.2.3.4"], []))
    apply = Mock()
    updater = bu.BogusUpdater(str(tmp_path), on_update=apply)
    updater._run_due()
    assert updater._tasks[0].next_due == now[0] + bu.RETRY_INTERVAL
    apply.assert_called_once_with(["10.0.0.99"], [])
    assert cache.read_bytes() == before
    assert updater.last_updated is None
    assert updater.force_update() is True


def test_bootstrap_bundle_does_not_count_as_network_success(tmp_path, monkeypatch):
    monkeypatch.setattr(bu, "_fetch_remote", Mock(side_effect=TimeoutError))
    monkeypatch.setattr(bu, "_load_from_local", lambda: (["1.2.3.4"], []))
    updater = bu.BogusUpdater(str(tmp_path))
    assert updater._try_update() is False
    assert bu.load_cached(str(tmp_path)) == (["1.2.3.4"], [])
    assert updater.last_updated is None


def test_network_success_and_disk_failure(tmp_path, monkeypatch):
    clock(monkeypatch)
    ips = [f"10.0.0.{i}" for i in range(1, 21)]
    monkeypatch.setattr(bu, "_fetch_remote", lambda *a, **k: json.dumps({"bogus_ips": ips}).encode())
    updater = bu.BogusUpdater(str(tmp_path))
    assert updater._try_update() is True
    assert updater.last_updated == 1_000_100
    assert bu.load_cached(str(tmp_path))[0] == ips
    monkeypatch.setattr(bu, "_save_cache", lambda *a: False)
    assert updater._try_update() is False
    assert updater.force_update() is False


def test_manual_and_periodic_bogus_updates_are_serialized(tmp_path, monkeypatch):
    updater = bu.BogusUpdater(str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    calls = []
    def update():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return True, True
    monkeypatch.setattr(updater, "_update_once", update)
    first = threading.Thread(target=updater._try_update)
    second = threading.Thread(target=updater.force_update)
    first.start()
    assert entered.wait(2)
    second.start()
    assert len(calls) == 1
    release.set()
    first.join(2)
    second.join(2)
    assert len(calls) == 2


def test_duplicate_task_rejected(tmp_path):
    updater = bu.BogusUpdater(str(tmp_path))
    with pytest.raises(ValueError):
        updater.add_task("bogus-IP", lambda: True)
