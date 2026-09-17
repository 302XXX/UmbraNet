"""
Тесты возврата DNS пользователю (P0-1, P0-2, P0-3).

Суть проблемы, которую закрывают эти тесты:

  P0-1. На выходе программа БЕЗУСЛОВНО сбрасывала системный DNS. Достаточно
        было открыть UmbraNet и закрыть — прописанные вручную DNS затирались
        на «Авто (DHCP)» безвозвратно.

  P0-2. Восстановление из снапшота было написано, но недоступно: функция
        network_restore_latest() не вызывалась нигде. Пользователь не мог
        вернуть свои настройки.

  P0-3. Watchdog на опросе PID не срабатывал при переиспользовании PID —
        у пользователя оставался мёртвый 127.0.0.1 и не было интернета.

Запуск: python -m pytest tests/test_dns_restore.py
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
for p in (str(CORE), str(CORE / "dns"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── P0-1: не трогаем DNS, который мы не меняли ───────────────────────────────

def test_hard_stop_does_not_touch_dns_if_we_never_changed_it():
    """ГЛАВНЫЙ фикс P0-1: если мы не меняли DNS, выход не должен его трогать."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    import umbranet.app as app_mod
    from umbranet.app import MainWindow

    win = MainWindow()
    calls = []
    try:
        # Подменяем реальные опасные операции «шпионами».
        app_mod.is_admin = lambda: True          # права есть, но DNS мы не меняли
        win._dns_was_set_by_app = False          # ← ключевое: мы НЕ меняли DNS
        app_mod.network_restore_latest = lambda: (calls.append("restore"), (True, ""))[1]
        win._shutdown_watchdog = lambda command="CLEAN": calls.append(f"wd:{command}")
        win._handoff_dns_restore_to_watchdog = lambda: calls.append("handoff") or True

        win._hard_stop_runtime(reset_dns=True)

        assert "restore" not in calls, (
            "DNS НЕЛЬЗЯ восстанавливать/сбрасывать, если мы его не меняли — "
            "иначе у пользователя затираются его собственные настройки"
        )
        assert "handoff" not in calls
        assert "wd:CLEAN" in calls, "watchdog должен получить CLEAN и уйти без действий"
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()


def test_hard_stop_restores_dns_when_we_did_change_it():
    """Обратная сторона: если DNS меняли — откат обязателен."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    import umbranet.app as app_mod
    from umbranet.app import MainWindow

    win = MainWindow()
    calls = []
    try:
        app_mod.is_admin = lambda: True
        win._dns_was_set_by_app = True
        win._handoff_dns_restore_to_watchdog = lambda: calls.append("handoff") or True

        win._hard_stop_runtime(reset_dns=True)
        assert "handoff" in calls, "при реально изменённом DNS откат должен происходить"
        assert win._dns_was_set_by_app is False, "флаг должен сброситься"
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()


def test_hard_stop_falls_back_to_sync_restore_without_watchdog():
    """Watchdog недоступен → откат делаем синхронно, чтобы не остаться без DNS.

    Лучше задержка на закрытии окна, чем мёртвый 127.0.0.1 у пользователя.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    import umbranet.app as app_mod
    from umbranet.app import MainWindow

    win = MainWindow()
    calls = []
    try:
        app_mod.is_admin = lambda: True
        app_mod.network_restore_latest = lambda: (calls.append("restore"),
                                                  (True, "Восстановлено: Ethernet"))[1]
        win._dns_was_set_by_app = True
        win._watchdog_proc = None        # watchdog не запущен/уже мёртв

        win._hard_stop_runtime(reset_dns=True)

        assert "restore" in calls, "без watchdog откат обязан выполниться синхронно"
        assert win._dns_was_set_by_app is False
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()


def test_hard_stop_without_reset_flag_does_nothing():
    """_hard_stop_runtime(reset_dns=False) — выходим в трей, DNS не трогаем."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.app import MainWindow

    win = MainWindow()
    calls = []
    try:
        win._dns_was_set_by_app = True
        win._handoff_dns_restore_to_watchdog = lambda: calls.append("handoff") or True
        win._shutdown_watchdog = lambda command="CLEAN": calls.append(f"wd:{command}")

        win._hard_stop_runtime(reset_dns=False)
        assert "handoff" not in calls
        assert "wd:CLEAN" in calls
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()


# ── P0-2: снапшот как источник «как было» ────────────────────────────────────

def test_dns_already_localhost_detection():
    """Предохранитель: не снимать снапшот, если DNS уже наш."""
    import network_repair as nr

    clean = lambda: {"Ethernet": {"ipv4": ["1.1.1.1"], "ipv6": []}}
    ours4 = lambda: {"Ethernet": {"ipv4": ["127.0.0.1", "8.8.8.8"], "ipv6": []}}
    ours6 = lambda: {"Ethernet": {"ipv4": [], "ipv6": ["::1"]}}
    empty = lambda: {}

    # На Linux функция отдаёт False без Windows — проверяем логику через
    # принудительный IS_WINDOWS, чтобы тест был осмысленным и в песочнице.
    orig = nr.IS_WINDOWS
    nr.IS_WINDOWS = True
    try:
        assert nr.dns_already_localhost(dns_getter=clean) is False
        assert nr.dns_already_localhost(dns_getter=ours4) is True
        assert nr.dns_already_localhost(dns_getter=ours6) is True
        assert nr.dns_already_localhost(dns_getter=empty) is False
    finally:
        nr.IS_WINDOWS = orig


def test_dns_already_localhost_fails_safe_when_unreadable():
    """Не смогли прочитать DNS → считаем, что уже вмешались (не портим снапшот)."""
    import network_repair as nr

    def boom():
        raise RuntimeError("powershell недоступен")

    orig = nr.IS_WINDOWS
    nr.IS_WINDOWS = True
    try:
        assert nr.dns_already_localhost(dns_getter=boom) is True
    finally:
        nr.IS_WINDOWS = orig


def test_restore_user_dns_uses_snapshot_when_available(tmp_path, monkeypatch):
    """Есть снапшот → восстанавливаем ИМЕННО его, а не DHCP."""
    import network_repair as nr

    snap = tmp_path / "network_snapshot_test.json"
    snap.write_text(json.dumps({
        "ok": True,
        "time_local": "2026-09-15 14:00:00",
        "adapters": {"Ethernet": {"ipv4": ["1.1.1.1"], "ipv6": []}},
    }, ensure_ascii=False), encoding="utf-8")

    called = {"restore": [], "dhcp": 0}
    monkeypatch.setattr(nr, "IS_WINDOWS", True)
    monkeypatch.setattr(nr, "restore_snapshot",
                        lambda path=None, ps_runner=None: (called["restore"].append(path), (True, "Восстановлено: Ethernet"))[1])
    monkeypatch.setattr(nr, "latest_snapshot", lambda: snap)

    ok, msg = nr.restore_user_dns()
    assert ok is True
    assert called["restore"] == [str(snap)], "должен восстанавливаться именно снапшот"
    assert "Ethernet" in msg


def test_restore_user_dns_falls_back_to_dhcp_without_snapshot(monkeypatch):
    """Нет снапшота → возвращаем DHCP, чтобы не оставить мёртвый 127.0.0.1."""
    import network_repair as nr
    import process_monitor

    monkeypatch.setattr(nr, "IS_WINDOWS", True)
    monkeypatch.setattr(nr, "latest_snapshot", lambda: None)
    monkeypatch.setattr(process_monitor, "reset_dns_to_auto",
                        lambda: (True, "DNS сброшен на DHCP: Ethernet"))

    ok, msg = nr.restore_user_dns()
    assert ok is True
    assert "DHCP" in msg


def test_restore_user_dns_falls_back_to_dhcp_when_snapshot_restore_fails(monkeypatch, tmp_path):
    """Снапшот есть, но восстановить не вышло → всё равно убираем наш 127.0.0.1.

    Худшее состояние — оставить пользователя без интернета вообще, поэтому
    «Авто» лучше, чем мёртвый локальный DNS.
    """
    import network_repair as nr
    import process_monitor

    # Каталог берём у pytest: жёсткий /tmp на Windows превращается в C:\tmp,
    # которого может не быть, и тест падал бы на создании файла.
    snap = tmp_path / "umbranet_test_snap.json"
    snap.write_text(json.dumps({"adapters": {"Ethernet": {"ipv4": ["1.1.1.1"]}}}),
                    encoding="utf-8")
    monkeypatch.setattr(nr, "IS_WINDOWS", True)
    monkeypatch.setattr(nr, "latest_snapshot", lambda: snap)
    monkeypatch.setattr(nr, "restore_snapshot",
                        lambda path=None, ps_runner=None: (False, "PowerShell упал"))
    monkeypatch.setattr(process_monitor, "reset_dns_to_auto",
                        lambda: (True, "DNS сброшен на DHCP"))

    ok, _msg = nr.restore_user_dns()
    assert ok is True, "после провала снапшота обязан сработать фолбэк на DHCP"


def test_restore_user_dns_empty_snapshot_means_dhcp(monkeypatch, tmp_path):
    """Пустой снапшот (у пользователя и был DHCP) → восстанавливать нечего."""
    import network_repair as nr
    import process_monitor

    snap = tmp_path / "umbranet_test_snap_empty.json"
    snap.write_text(json.dumps({"adapters": {}}), encoding="utf-8")
    monkeypatch.setattr(nr, "IS_WINDOWS", True)
    monkeypatch.setattr(nr, "latest_snapshot", lambda: snap)
    monkeypatch.setattr(process_monitor, "reset_dns_to_auto",
                        lambda: (True, "DNS сброшен на DHCP"))

    ok, _msg = nr.restore_user_dns()
    assert ok is True


def test_restore_dialog_warns_that_bypass_stops():
    """Диалог отката обязан предупредить: обход выключится, нужен «Старт» заново."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    import umbranet.views.network as net_mod

    view = net_mod.NetworkView()
    try:
        # 1) Программа работает + снапшот есть
        view.engine.running = True
        net_mod.network_snapshot_info = lambda: {
            "exists": True, "time_local": "2026-09-15 14:02:11", "adapters": 2}
        text = view._restore_confirm_text()
        assert "2026-09-15 14:02:11" in text, "должна быть дата снапшота"
        assert "2 адапт" in text
        assert "Старт" in text, "обязательно предупредить про повторный Старт"
        assert "маршрутизация" in text.lower()

        # 2) Программа остановлена — предупреждение про Старт не нужно
        view.engine.running = False
        text_off = view._restore_confirm_text()
        assert "Старт" not in text_off
        assert "2026-09-15 14:02:11" in text_off

        # 3) Снапшота нет — честно говорим про сброс на Авто
        net_mod.network_snapshot_info = lambda: {"exists": False, "adapters": 0}
        text_none = view._restore_confirm_text()
        assert "Авто (DHCP)" in text_none
        assert "Снапшот не найден" in text_none
    finally:
        view.deleteLater()
        app.processEvents()


def test_snapshot_info_reports_existence(tmp_path, monkeypatch):
    """UI-тултип должен знать, есть ли снапшот и от какого он времени."""
    import network_repair as nr

    missing = nr.snapshot_info(path=str(tmp_path / "nope.json"))
    assert missing["exists"] is False

    snap = tmp_path / "network_snapshot_x.json"
    snap.write_text(json.dumps({
        "time_local": "2026-09-15 14:02:11",
        "adapters": {"Ethernet": {}, "Wi-Fi": {}},
    }, ensure_ascii=False), encoding="utf-8")
    info = nr.snapshot_info(path=str(snap))
    assert info["exists"] is True
    assert info["time_local"] == "2026-09-15 14:02:11"
    assert info["adapters"] == 2


# ── P0-3: watchdog на pipe вместо опроса PID ─────────────────────────────────

def _load_watchdog():
    import importlib.util
    path = CORE / "watchdog.py"
    spec = importlib.util.spec_from_file_location("umbranet_watchdog_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _no_sleep(_seconds):
    return None


def test_watchdog_clean_after_hello_does_nothing():
    """HELLO + CLEAN: родитель сам всё вернул — watchdog не трогает сеть."""
    wd = _load_watchdog()
    sig = wd.wait_for_parent(io.BytesIO(b"HELLO\nCLEAN\n"), sleep_fn=_no_sleep)
    assert sig == wd.SIGNAL_CLEAN

    called = []
    assert wd.handle_signal(sig, restore_fn=lambda: called.append(1)) == "skip"
    assert called == [], "при CLEAN сеть трогать нельзя"


def test_watchdog_restore_after_hello_restores():
    """HELLO + RESTORE: родитель просит вернуть DNS (не морозя свой UI)."""
    wd = _load_watchdog()
    sig = wd.wait_for_parent(io.BytesIO(b"HELLO\nRESTORE\n"), sleep_fn=_no_sleep)
    assert sig == wd.SIGNAL_RESTORE

    called = []
    assert wd.handle_signal(sig, restore_fn=lambda: called.append(1)) == "restore"
    assert called == [1]


def test_watchdog_eof_after_hello_means_parent_died():
    """ГЛАВНЫЙ фикс P0-3: HELLO, затем EOF (родитель упал) → спасаем интернет.

    Раньше здесь был опрос tasklist, который при переиспользовании PID считал
    мёртвого родителя живым и не срабатывал никогда.
    """
    wd = _load_watchdog()
    sig = wd.wait_for_parent(io.BytesIO(b"HELLO\n"), sleep_fn=_no_sleep)
    assert sig == "", "EOF после HELLO должен давать пустой сигнал"

    called = []
    assert wd.handle_signal(sig, restore_fn=lambda: called.append(1)) == "restore"
    assert called == [1], "при смерти родителя DNS обязан вернуться"


def test_watchdog_eof_immediately_means_parent_died_at_start():
    """Родитель умер, не успев поздороваться — всё равно возвращаем DNS."""
    wd = _load_watchdog()
    called = []
    sig = wd.wait_for_parent(io.BytesIO(b""), sleep_fn=_no_sleep)
    wd.handle_signal(sig, restore_fn=lambda: called.append(1))
    assert called == [1]


def test_watchdog_keeps_leftover_buffer():
    """HELLO и CLEAN могут прийти одной пачкой — вторую строку терять нельзя.

    Без сохранения остатка буфера watchdog решил бы, что родитель умер, и
    сбросил бы DNS живого приложения (регресс, пойманный при разработке).
    """
    wd = _load_watchdog()
    assert wd.wait_for_parent(io.BytesIO(b"HELLO\nCLEAN\ngarbage\n"),
                              sleep_fn=_no_sleep) == wd.SIGNAL_CLEAN


def test_watchdog_signal_is_robust():
    """Регистр, CRLF и пробелы не должны ломать разбор."""
    wd = _load_watchdog()
    assert wd.read_parent_signal(io.BytesIO(b"hello\r\n")) == wd.SIGNAL_HELLO
    assert wd.read_parent_signal(io.BytesIO(b"  CLEAN")) == wd.SIGNAL_CLEAN
    assert wd.read_parent_signal(io.BytesIO(b"RESTORE")) == wd.SIGNAL_RESTORE
    assert wd.read_parent_signal(io.BytesIO(b"")) == ""
    assert wd.read_parent_signal(io.BytesIO(b"garbage\n")) == ""


def test_watchdog_unmonitored_never_touches_dns():
    """Если канал не заработал — выходим НЕ трогая DNS.

    Это правило безопасности: сброс DNS работающего UmbraNet отключил бы
    пользователю интернет. «Не смог проверить» ≠ «родитель умер».
    """
    wd = _load_watchdog()

    class Broken:
        def read(self, _n=4096):
            raise OSError("pipe сломан")

    with pytest.raises(wd.UnmonitoredError):
        wd.wait_for_parent(Broken(), sleep_fn=_no_sleep)


def test_watchdog_garbage_then_eof_is_death():
    """Мусор в канале, затем настоящий EOF → родитель мёртв, DNS возвращаем.

    Разбор случая: непонятная строка сама по себе смертью не считается (мы
    ждём дальше), но если после неё канал чист закрылся — родителя больше нет.
    Оставить в этой ситуации 127.0.0.1 = оставить человека без интернета.
    """
    wd = _load_watchdog()
    sig = wd.wait_for_parent(io.BytesIO(b"\x00\xff nonsense\n"), sleep_fn=_no_sleep)
    assert sig == ""

    called = []
    wd.handle_signal(sig, restore_fn=lambda: called.append(1))
    assert called == [1]


def test_watchdog_unknown_line_with_live_parent_waits():
    """Непонятная строка ПРИ ЖИВОМ родителе: не считаем его мёртвым.

    Если бы мусор сразу трактовался как смерть, любой сбой форматирования
    приводил бы к ложному сбросу DNS работающего UmbraNet.
    """
    wd = _load_watchdog()
    calls = {"read": 0}

    class Chatty:
        """Отдаёт мусор, потом честные HELLO и CLEAN (родитель жив)."""

        def __init__(self):
            self._parts = [b"garbage\n", b"HELLO\n", b"CLEAN\n"]

        def read(self, _n=4096):
            calls["read"] += 1
            return self._parts.pop(0) if self._parts else b""

    sig = wd.wait_for_parent(Chatty(), sleep_fn=_no_sleep)
    assert sig == wd.SIGNAL_CLEAN, "после мусора надо дождаться HELLO, а не хоронить родителя"


def test_watchdog_uses_raw_fd_not_sys_stdin():
    """Под pythonw.exe sys.stdin is None — читать его нельзя.

    Если бы watchdog полагался на sys.stdin, он увидел бы «пустоту», решил, что
    родитель мёртв, и сбросил DNS сразу после старта, сломав маршрутизацию.
    """
    src = (CORE / "watchdog.py").read_text(encoding="utf-8")
    assert "_FdStream" in src, "нужен фолбэк на сырой fd 0"
    assert "os.read" in src
    # sys.stdin допустим только как «если есть», с фолбэком на fd
    assert 'getattr(sys.stdin, "buffer", None) or _FdStream(0)' in src


def test_watchdog_has_no_print_without_stdout_guard():
    """Под pythonw.exe нет и sys.stdout — прямой print() уронил бы watchdog."""
    src = (CORE / "watchdog.py").read_text(encoding="utf-8")
    assert "_say" in src, "нужен безопасный вывод"
    rest = src.split("def is_admin", 1)[1]
    assert "\n    print(" not in rest, "в коде не должно быть прямых print()"


def test_app_sends_hello_handshake():
    """Родитель обязан поздороваться, иначе watchdog не отличит «молчит» от «умер»."""
    src = (ROOT / "umbranet" / "app.py").read_text(encoding="utf-8")
    assert b"HELLO".decode() in src, "app.py должен отправлять HELLO в stdin watchdog"
    assert "stdin=subprocess.PIPE" in src, "watchdog надо запускать с pipe на stdin"


def test_app_hands_dns_restore_to_watchdog():
    """ГЛАВНЫЙ фикс P0-1/P0-2: условие отката требует _dns_was_set_by_app."""
    src = (ROOT / "umbranet" / "app.py").read_text(encoding="utf-8")
    assert ("restore_needed = bool(reset_dns and self._dns_was_set_by_app and is_admin())"
            in src), "откат DNS обязан проверять, что DNS меняли именно мы"
