"""
Тесты индикатора «кто отвечает» (H3) — fallback-транспорт, провайдер, системный DNS.

Жалоба по факту аудита: пользователь не видит, что запрос ушёл запасным путём.
В резолвере таких путей три, и все три раньше были видны только в текстовом логе:

  1. запасной ТРАНСПОРТ — выбран DoH, но он не ответил, и запрос ушёл открытым
     UDP (провайдер видит и может подменить);
  2. запасной ПРОВАЙДЕР — xbox-dns не ответил ни одним транспортом, отвечает
     comss.one или профиль пользователя;
  3. СИСТЕМНЫЙ DNS — обычный путь для доменов вне списка маршрутизации; считать
     его «поломкой» нельзя, но долю таких запросов надо видеть.

Тесты проверяют:
  • сам трекер (`core/dns/fallback_state.py`) — счётчики, вердикт, потокобезопасность;
  • что резолвер РЕАЛЬНО пишет в трекер (запасной транспорт, запасной провайдер,
    системный DNS) и что служебные пробы не портят статистику;
  • что карточка в «Сети и диагностике» показывает правду, а не выдумку.

Запуск: python -m pytest tests/test_fallback_indicator.py
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dns"), str(ROOT / "core" / "dpi")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture()
def tracker():
    """Свежий трекер: состояние — singleton в памяти, тесты не должны влиять друг на друга."""
    import fallback_state

    fallback_state.reset_fallback_state()
    yield fallback_state.get_fallback_state()
    fallback_state.reset_fallback_state()


# ── 1. Трекер ───────────────────────────────────────────────────────────────

def test_primary_answer_is_not_a_fallback(tracker):
    tracker.record_provider_answer("xbox", "xbox-dns.ru", "doh", "doh", provider_index=0)
    snap = tracker.snapshot()
    assert snap["state"] == "primary"
    assert snap["fallback_transport"] is False
    assert snap["fallback_provider"] is False
    assert snap["encrypted"] is True, "DoH обязан считаться шифрованным"


def test_transport_fallback_is_detected(tracker):
    """Выбран DoH, ответил UDP → это запасной транспорт, и он открытый."""
    tracker.record_provider_answer("xbox", "xbox-dns.ru", "udp", "doh")
    snap = tracker.snapshot()
    assert snap["state"] == "fallback"
    assert snap["fallback_transport"] is True
    assert snap["encrypted"] is False
    assert snap["counters"]["transport_fallback"] == 1
    assert snap["counters"]["plain"] == 1


def test_provider_fallback_is_detected_and_stronger_than_transport(tracker):
    """Запасной провайдер — более серьёзный случай, он и должен быть вердиктом."""
    tracker.record_provider_answer("comss", "comss.one", "doh", "doh",
                                   provider_index=1, is_primary=False)
    snap = tracker.snapshot()
    assert snap["state"] == "fallback_provider"
    assert snap["fallback_provider"] is True
    assert snap["counters"]["provider_fallback"] == 1


def test_system_answers_counted_separately(tracker):
    """Системный DNS считается отдельно: это не запасной провайдер."""
    tracker.record_provider_answer("xbox", "xbox-dns.ru", "doh", "doh")
    tracker.record_system_answer("example.com")
    snap = tracker.snapshot()
    assert snap["counters"]["answers"] == 1
    assert snap["counters"]["system_answers"] == 1
    assert snap["system_share"] == pytest.approx(0.5)
    # Заголовок индикатора остаётся про профили UmbraNet: системный DNS —
    # обычный путь для доменов вне списка маршрутизации, а не «запасной».
    assert snap["state"] == "primary"
    assert snap["last_system_domain"] == "example.com"


def test_idle_state_before_any_answers(tracker):
    snap = tracker.snapshot()
    assert snap["state"] == "idle"
    assert snap["counters"]["answers"] == 0
    assert snap["system_share"] == 0.0


def test_system_only_state_when_profiles_are_silent(tracker):
    """Если отвечал только системный DNS, это отдельное состояние, а не «запасной провайдер»."""
    tracker.record_system_answer("example.com")
    snap = tracker.snapshot()
    assert snap["state"] == "system_only"
    assert snap["counters"]["answers"] == 0
    assert snap["counters"]["system_answers"] == 1


def test_reset_clears_counters(tracker):
    tracker.record_provider_answer("xbox", "xbox-dns.ru", "udp", "doh")
    tracker.record_system_answer("example.com")
    tracker.reset()
    snap = tracker.snapshot()
    assert snap["state"] == "idle"
    assert snap["counters"]["answers"] == 0
    assert snap["counters"]["system_answers"] == 0


def test_tracker_is_thread_safe(tracker):
    """DNS-запросы идут из нескольких потоков — счётчики не должны теряться."""
    def hammer(n: int):
        for _ in range(n):
            tracker.record_provider_answer("xbox", "xbox-dns.ru", "doh", "doh")
            tracker.record_system_answer("x.com")

    threads = [threading.Thread(target=hammer, args=(200,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 5 потоков × 200 итераций: в каждой один ответ провайдера и один системный.
    snap = tracker.snapshot()
    assert snap["counters"]["answers"] == 1000
    assert snap["counters"]["system_answers"] == 1000


def test_snapshot_is_a_copy(tracker):
    """UI читает снимок в другом потоке — правки снимка не должны ломать трекер."""
    tracker.record_provider_answer("xbox", "xbox-dns.ru", "doh", "doh")
    snap = tracker.snapshot()
    snap["counters"]["answers"] = 999
    assert tracker.snapshot()["counters"]["answers"] == 1


# ── 2. Резолвер пишет правду ────────────────────────────────────────────────

def _fake_request():
    from dnslib import DNSRecord
    return DNSRecord.question("chatgpt.com", "A")


def _fake_response():
    from dnslib import DNSRecord
    return DNSRecord.question("chatgpt.com", "A").reply()


def test_resolver_records_primary_transport(tracker, monkeypatch):
    """Ответил первый транспорт первого провайдера → штатный путь, без «запасного»."""
    import dns_server as ds

    monkeypatch.setattr(ds, "get_failover_providers",
                        lambda config: [{"id": "xbox", "name": "xbox-dns.ru"}])
    monkeypatch.setattr(ds, "_xbox_transport", lambda name: (lambda *a, **k: _fake_response()))

    response = ds.resolve_via_xbox(_fake_request(), {"xbox_dns_mode": "doh"})

    assert response is not None
    snap = tracker.snapshot()
    assert snap["provider_name"] == "xbox-dns.ru"
    assert snap["transport"] == "doh"
    assert snap["state"] == "primary"


def test_resolver_records_transport_fallback(tracker, monkeypatch):
    """DoH не ответил, ответил UDP → в статистике это запасной транспорт."""
    import dns_server as ds

    def resolver(name):
        if name == "udp":
            return lambda *a, **k: _fake_response()
        return lambda *a, **k: None

    monkeypatch.setattr(ds, "get_failover_providers",
                        lambda config: [{"id": "xbox", "name": "xbox-dns.ru"}])
    monkeypatch.setattr(ds, "_xbox_transport", resolver)

    assert ds.resolve_via_xbox(_fake_request(), {"xbox_dns_mode": "doh"}) is not None
    snap = tracker.snapshot()
    assert snap["transport"] == "udp"
    assert snap["preferred_transport"] == "doh"
    assert snap["fallback_transport"] is True
    assert snap["state"] == "fallback"


def test_resolver_records_provider_fallback(tracker, monkeypatch):
    """Первый провайдер молчит, отвечает запасной → пользователь должен это видеть."""
    import dns_server as ds

    def resolver(name):
        return lambda *a, **k: _fake_response()

    def providers_of(config):
        return [{"id": "xbox", "name": "xbox-dns.ru"}, {"id": "comss", "name": "comss.one"}]

    calls = {"n": 0}

    def provider_resolver(request, config, profile, transport_order):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, ""                     # основной провайдер молчит
        return _fake_response(), transport_order[0]

    monkeypatch.setattr(ds, "get_failover_providers", providers_of)
    monkeypatch.setattr(ds, "_resolve_via_provider", provider_resolver)

    assert ds.resolve_via_xbox(_fake_request(), {"xbox_dns_mode": "doh"}) is not None
    snap = tracker.snapshot()
    assert snap["provider_name"] == "comss.one"
    assert snap["fallback_provider"] is True
    assert snap["state"] == "fallback_provider"


def test_service_probe_does_not_touch_statistics(tracker, monkeypatch):
    """Служебная проба (AAAA для приоритета IPv6) не должна попадать в индикатор.

    Иначе «Сейчас отвечает» показывал бы не тот ответ, который получил клиент.
    """
    import dns_server as ds

    monkeypatch.setattr(ds, "get_failover_providers",
                        lambda config: [{"id": "xbox", "name": "xbox-dns.ru"}])
    monkeypatch.setattr(ds, "_xbox_transport", lambda name: (lambda *a, **k: _fake_response()))

    ds.resolve_via_xbox(_fake_request(), {"xbox_dns_mode": "doh"}, record=False)

    snap = tracker.snapshot()
    assert snap["state"] == "idle", "проба не должна выглядеть как настоящий ответ"
    assert snap["counters"]["answers"] == 0


def test_ipv6_priority_probe_is_not_recorded(tracker, monkeypatch):
    """Проба AAAA для «приоритета IPv6» идёт ЧЕРЕЗ реальную ветку резолвера.

    Это служебный запрос: клиенту он не отдаётся. Если записать его в статистику,
    карточка «Сейчас отвечает» покажет ответ, которого пользователь не получал.
    Проверяем именно вызов из `_resolve_routed` — там легко потерять record=False.
    """
    import dns_server as ds
    from dnslib import AAAA, QTYPE, RR

    seen: list[bool] = []

    def spy(request, config, record=True):
        seen.append(record)
        resp = _fake_response()
        resp.add_answer(RR("chatgpt.com", QTYPE.AAAA, rdata=AAAA("2001:db8::1")))
        return resp

    monkeypatch.setattr(ds, "resolve_via_xbox", spy)
    config = {"ipv6_priority_enabled": True, "routed_cache_enabled": False}
    resolver = ds.UmbraNetResolver([config], cache=_FakeCache())

    reply = resolver._resolve_routed(_fake_request(), "chatgpt.com", "A", routed=True)

    assert reply is not None, "ветка приоритета IPv6 не отработала"
    assert seen == [False], f"проба AAAA должна идти без записи в статистику, получено {seen}"
    assert tracker.snapshot()["state"] == "idle", "служебная проба попала в индикатор"


def test_system_dns_recorded_on_real_answer(tracker, monkeypatch):
    """Ответ через системный DNS фиксируется отдельным счётчиком."""
    import dns_server as ds

    resolver = ds.UmbraNetResolver([{"xbox_dns_mode": "doh"}], cache=_FakeCache())
    monkeypatch.setattr(ds.UmbraNetResolver, "_system_with_bogus_check",
                        lambda self, request, domain, qtype: _fake_response())
    monkeypatch.setattr(ds.UmbraNetResolver, "_stale_ttl", lambda self: 0)

    reply = resolver._resolve_system(_fake_request(), "example.com", "A", routed=False)

    assert reply is not None
    snap = tracker.snapshot()
    assert snap["counters"]["system_answers"] == 1
    assert snap["last_system_domain"] == "example.com"
    assert snap["state"] == "system_only", "профили молчали — так и должно быть в вердикте"


class _FakeCache:
    """Кэш-заглушка: всегда промах, чтобы тест шёл в апстрим."""

    def get_with_state(self, request, routed):
        return None, "miss"

    def set(self, *a, **k):
        return None


# ── 3. Карточка в интерфейсе ───────────────────────────────────────────────

# Приложение Qt держим на уровне модуля: если объект останется без ссылки,
# сборщик мусора уничтожит его и следующий тест упадёт.
_QAPP = None


def _ui():
    """Поднимает QApplication в offscreen-режиме (нужен для окна диагностики)."""
    import os

    global _QAPP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    _QAPP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_card_shows_primary_answer():
    """Штатный путь: зелёная строка с провайдером и транспортом."""
    _ui()
    import fallback_state

    from umbranet.views.network import NetworkView

    fallback_state.reset_fallback_state()
    fallback_state.get_fallback_state().record_provider_answer(
        "xbox", "xbox-dns.ru", "doh", "doh")
    view = NetworkView()
    view._refresh_answering()
    assert "🟢" in view._answer_state.text()
    assert "xbox-dns.ru" in view._answer_state.text()
    assert "DoH" in view._answer_state.text()
    view.deleteLater()


def test_card_warns_about_transport_fallback():
    """Запасной транспорт: жёлтая строка и объяснение, что запрос ушёл открытым."""
    _ui()
    import fallback_state

    from umbranet.views.network import NetworkView

    fallback_state.reset_fallback_state()
    fallback_state.get_fallback_state().record_provider_answer(
        "xbox", "xbox-dns.ru", "udp", "doh")
    view = NetworkView()
    view._refresh_answering()
    assert "🟡" in view._answer_state.text()
    assert "UDP" in view._answer_state.text()
    details = view._answer_details.text()
    assert "DoH" in details and "UDP" in details, "должно быть сказано, ЧТО заменили"
    view.deleteLater()


def test_card_warns_about_provider_fallback():
    _ui()
    import fallback_state

    from umbranet.views.network import NetworkView

    fallback_state.reset_fallback_state()
    fallback_state.get_fallback_state().record_provider_answer(
        "comss", "comss.one", "doh", "doh", provider_index=1, is_primary=False)
    view = NetworkView()
    view._refresh_answering()
    assert "запасной провайдер" in view._answer_state.text().lower()
    assert "comss.one" in view._answer_state.text()
    view.deleteLater()


def test_card_counts_system_dns_share():
    """Доля системного DNS видна и объяснена как норма для доменов вне списка."""
    _ui()
    import fallback_state

    from umbranet.views.network import NetworkView

    fs = fallback_state.get_fallback_state()
    fallback_state.reset_fallback_state()
    fs.record_provider_answer("xbox", "xbox-dns.ru", "doh", "doh")
    fs.record_system_answer("example.com")
    fs.record_provider_answer("xbox", "xbox-dns.ru", "doh", "doh")
    fs.record_system_answer("example.org")
    fs.record_system_answer("example.net")
    view = NetworkView()
    view._refresh_answering()
    counters = view._answer_counters.text()
    assert "системный DNS" in counters
    assert "60%" in counters, f"доля системного DNS не посчитана: {counters}"
    assert "вне списка маршрутизации" in counters, "нужно объяснение, а не только цифра"
    view.deleteLater()


def test_card_says_no_data_instead_of_guessing():
    """Ядро недоступно (заглушка / нет статистики): карточка говорит прямо, без выдумок."""
    _ui()

    import umbranet.engine_adapter as ea
    from umbranet.views.network import NetworkView

    def _no_data():
        return {}

    view = NetworkView()
    original = ea.fallback_status
    ea.fallback_status = _no_data
    try:
        view._refresh_answering()
        assert "нет" in view._answer_state.text().lower(), view._answer_state.text()
        assert view._answer_counters.text() == ""
    finally:
        ea.fallback_status = original
        view.deleteLater()


def test_card_says_idle_when_no_queries_yet():
    """Запросов ещё не было — это не «всё хорошо» и не «авария», а «пока тихо»."""
    _ui()
    import fallback_state

    from umbranet.views.network import NetworkView

    fallback_state.reset_fallback_state()
    view = NetworkView()
    view._refresh_answering()
    assert "не было" in view._answer_state.text().lower()
    view.deleteLater()


def test_ui_reads_state_through_adapter():
    """Интерфейс ходит за состоянием через адаптер (контракт UI ↔ ядро), а не в обход."""
    source = (ROOT / "umbranet" / "views" / "network.py").read_text(encoding="utf-8")
    assert "fallback_status()" in source, "карточка должна читать состояние через адаптер"
    adapter = (ROOT / "umbranet" / "engine_adapter.py").read_text(encoding="utf-8")
    assert "def fallback_status()" in adapter, "адаптер обязан предоставить один вызов для UI"
