"""
Тесты DNS-кэша — `core/dns/dns_cache.py` (пункт 10 плана).
==========================================================

Модуль — сердце «оптимистичного» кэша: свежий ответ отдаём как есть, просроченный
в пределах stale-окна отдаём моментально и обновляем в фоне, совсем старое
выкидываем. Ошибка здесь видна пользователю сразу: либо ответы «залипают»
устаревшими, либо кэш отдаёт то, чего уже нет.

Проверяем: свежесть/просрочку/stale-окно по трём состояниям, уменьшение TTL на
возраст записи (и принудительный TTL=1 для stale-ответа), дедупликацию фоновых
обновлений, разбор TTL из ответа и переопределение TTL, вытеснение при переполнении,
уборку просроченного и работу фонового уборщика.

Время подменяется целиком (модуль видит свой «time»), поэтому тесты мгновенные.

Запуск: python -m pytest tests/test_dns_cache.py
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dns")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

dnslib = pytest.importorskip("dnslib")
import dns_cache
from dns_cache import DNSCache
from dnslib import QTYPE, RR, A, DNSRecord


class _Clock:
    """Виртуальные монотонные часы: подменяем модулю целиком, а не глобальный time."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


@pytest.fixture
def clock(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(dns_cache, "time", clk)
    return clk


@pytest.fixture
def cache():
    return DNSCache(max_entries=100, janitor_interval=1.0)


def _request(domain: str = "example.com", qtype: str = "A"):
    # dnslib ждёт тип запроса СТРОКОЙ («A», «AAAA»), а не числом QTYPE
    return DNSRecord.question(domain, qtype=qtype)


def _response(domain: str = "example.com", ttl: int = 60, ip: str = "93.184.216.34"):
    """Ответ с одной A-записью и заданным TTL."""
    reply = DNSRecord.question(domain).reply()
    reply.add_answer(RR(domain, QTYPE.A, rdata=A(ip), ttl=ttl))
    return reply


def _ttl_of(response) -> int:
    return int(response.rr[0].ttl)


# ── Три состояния записи ────────────────────────────────────────────────────

def test_fresh_answer_is_returned_as_is(cache, clock):
    """Свежий ответ отдаётся, TTL уменьшен на возраст записи."""
    request = _request()
    cache.set(request, False, _response(ttl=60))

    clock.advance(10)
    response, state = cache.get_with_state(request, False)

    assert state == "fresh", f"свежая запись названа {state}"
    assert _ttl_of(response) == 50, f"TTL не уменьшен на возраст: {_ttl_of(response)}"
    assert cache.get(request, False) is not None, "старое API get() не отдало свежий ответ"


def test_stale_answer_is_usable_and_marked(cache, clock):
    """Просроченный, но в stale-окне: отдаём и помечаем stale (клиент переспросит)."""
    request = _request()
    cache.set(request, False, _response(ttl=10), stale_ttl=3600)

    clock.advance(11)
    response, state = cache.get_with_state(request, False)

    assert state == "stale", f"просроченная запись названа {state}"
    assert _ttl_of(response) == 1, (
        f"stale-ответ отдаётся с TTL {_ttl_of(response)} — клиент закеширует просроченное"
    )


def test_completely_expired_answer_is_dropped(cache, clock):
    """После stale-окна запись выбрасывается: ни get(), ни get_with_state() её не видят."""
    request = _request()
    cache.set(request, False, _response(ttl=10), stale_ttl=5)

    clock.advance(16)
    assert cache.get_with_state(request, False) == (None, "miss")
    assert cache.get(request, False) is None


def test_old_api_get_does_not_return_stale(cache, clock):
    """get() сознательно не отдаёт stale: этим занимается get_with_state()."""
    request = _request()
    cache.set(request, False, _response(ttl=10), stale_ttl=3600)

    clock.advance(11)
    assert cache.get(request, False) is None, "get() отдал просроченный ответ"
    assert cache.get_with_state(request, False)[1] == "stale", "запись потерялась раньше времени"


def test_answer_is_copied_not_shared(cache, clock):
    """Кэш отдаёт копию: правка TTL клиентом не портит запись в кэше."""
    request = _request()
    cache.set(request, False, _response(ttl=60))

    first = cache.get(request, False)
    _ttl_of(first)
    first.rr[0].ttl = 1                        # «испортили» выданный ответ

    clock.advance(5)
    second = cache.get(request, False)
    assert _ttl_of(second) == 55, (
        f"в кэше хранится испорченный ответ (TTL {_ttl_of(second)} вместо 55) — нужен deepcopy"
    )


# ── Ключ кэша ───────────────────────────────────────────────────────────────

def test_key_ignores_case_and_trailing_dot(cache):
    """Один и тот же домен в разном регистре и с точкой на конце — одна запись."""
    cache.set(_request("Example.COM."), False, _response(ttl=60))
    answer, state = cache.get_with_state(_request("example.com"), False)
    assert state == "fresh", "регистр/точка в конце домена ломают попадание в кэш"
    assert answer is not None


def test_key_separates_routed_and_plain(cache):
    """routed и обычный маршрут не должны смешиваться в кэше."""
    cache.set(_request(), False, _response(ttl=60, ip="1.1.1.1"))

    assert cache.get_with_state(_request(), True) == (None, "miss"), (
        "ответ обычного маршрута выдан для routed-запроса"
    )


def test_key_separates_query_types(cache):
    """A и AAAA кэшируются раздельно."""
    cache.set(_request("example.com", "A"), False, _response(ttl=60))
    assert cache.get_with_state(_request("example.com", "AAAA"), False) == (None, "miss")


# ── TTL ─────────────────────────────────────────────────────────────────────

def test_ttl_is_minimum_across_sections(cache, clock):
    """TTL записи кэша — самый короткий из ответа: не держим дольше короткой записи."""
    reply = DNSRecord.question("example.com").reply()
    reply.add_answer(RR("example.com", QTYPE.A, rdata=A("1.1.1.1"), ttl=300))
    reply.add_answer(RR("example.com", QTYPE.A, rdata=A("1.1.1.2"), ttl=30))

    cache.set(_request(), False, reply)
    clock.advance(29)
    assert cache.get_with_state(_request(), False)[1] == "fresh"
    clock.advance(2)                            # 31 с — уже просрочено (TTL 30)
    assert cache.get_with_state(_request(), False) == (None, "miss")


def test_ttl_override_caps_response_ttl(cache, clock):
    """ttl_override ограничивает TTL сверху, если он короче ответа."""
    cache.set(_request(), False, _response(ttl=300), ttl_override=5)

    clock.advance(6)
    assert cache.get_with_state(_request(), False) == (None, "miss"), (
        "ttl_override не сработал: запись живёт дольше заданного"
    )


def test_ttl_override_is_used_when_response_has_no_ttl(cache, clock):
    """Ответ без TTL (0) с ttl_override живёт заданное время, а не «минималку» в 1 с."""
    reply = DNSRecord.question("example.com").reply()
    reply.add_answer(RR("example.com", QTYPE.A, rdata=A("1.1.1.1"), ttl=0))
    cache.set(_request(), False, reply, ttl_override=30)

    clock.advance(10)
    assert cache.get_with_state(_request(), False)[1] == "fresh", "ответ без TTL прожил всего 1 с"

    clock.advance(25)
    assert cache.get_with_state(_request(), False) == (None, "miss")


def test_broken_ttl_override_is_ignored(cache):
    """Мусор вместо TTL не ломает запись — берём TTL ответа."""
    cache.set(_request(), False, _response(ttl=60), ttl_override="не число")
    assert cache.get_with_state(_request(), False)[1] == "fresh"

    cache.set(_request("other.com"), False, _response(ttl=60), ttl_override=-5)
    assert cache.get_with_state(_request("other.com"), False)[1] == "fresh", (
        "отрицательный ttl_override испортил запись"
    )


def test_broken_stale_ttl_means_no_stale_window(cache, clock):
    """Мусор в stale_ttl трактуется как 0: окна «просроченное, но годное» нет.

    Так безопаснее: значение приходит из конфига, и лучше не растянуть окно на
    неведомо сколько, чем отдать устаревший ответ спустя часы. Важно, что запись
    при этом не ломается и ведёт себя как при stale_ttl=0.
    """
    cache.set(_request(), False, _response(ttl=10), stale_ttl="ой")

    clock.advance(5)
    assert cache.get_with_state(_request(), False)[1] == "fresh", "мусор сломал обычный TTL"
    clock.advance(6)
    assert cache.get_with_state(_request(), False) == (None, "miss"), (
        "при мусорном stale_ttl запись осталась жить после TTL"
    )


# ── Фоновое обновление ──────────────────────────────────────────────────────

def test_refreshing_flag_is_atomic(cache):
    """Пометить обновление можно один раз: второй запрос не запускает второй поток."""
    request = _request()
    cache.set(request, False, _response(ttl=60))

    assert cache.mark_refreshing(request, False) is True
    assert cache.mark_refreshing(request, False) is False, "запущено два обновления одного ключа"

    cache.unmark_refreshing(request, False)
    assert cache.mark_refreshing(request, False) is True, "после снятия метки обновление не идёт"


def test_refreshing_requires_existing_entry(cache):
    """Пометить можно только существующую запись: обновлять «ничего» нельзя."""
    assert cache.mark_refreshing(_request("нет-такого.example"), False) is False


def test_refreshing_status_is_kept_per_key(cache):
    """Метка обновления не мешает другому домену обновляться параллельно."""
    first, second = _request("a.example"), _request("b.example")
    cache.set(first, False, _response("a.example", ttl=60))
    cache.set(second, False, _response("b.example", ttl=60))

    assert cache.mark_refreshing(first, False) is True
    assert cache.mark_refreshing(second, False) is True, "чужое обновление заблокировало домен"


# ── Предел размера и уборка ─────────────────────────────────────────────────

def test_overflow_drops_oldest_entries(cache):
    """При переполнении вытесняются самые старые записи, свежие остаются."""
    small = DNSCache(max_entries=3, janitor_interval=1.0)
    for i in range(3):
        small.set(_request(f"site{i}.example"), False, _response(f"site{i}.example", ttl=600))
    small.set(_request("newest.example"), False, _response("newest.example", ttl=600))

    assert len(small) == 3, f"лимит не работает: в кэше {len(small)} записей"
    assert small.get_with_state(_request("newest.example"), False)[1] == "fresh", (
        "вытеснили только что добавленную запись"
    )
    assert small.get_with_state(_request("site0.example"), False) == (None, "miss"), (
        "самая старая запись не вытеснена"
    )


def test_expired_entries_are_pruned_first(cache, clock):
    """Истёкшие записи уходят раньше живых — место не тратится на мусор."""
    small = DNSCache(max_entries=2, janitor_interval=1.0)
    small.set(_request("dead.example"), False, _response("dead.example", ttl=5))
    clock.advance(6)                            # запись истекла полностью
    small.set(_request("a.example"), False, _response("a.example", ttl=600))
    small.set(_request("b.example"), False, _response("b.example", ttl=600))

    assert small.get_with_state(_request("dead.example"), False) == (None, "miss")
    assert small.get_with_state(_request("a.example"), False)[1] == "fresh", (
        "живая запись вытеснена вместо истёкшей"
    )


def test_no_limit_when_zero(cache):
    """max_entries=0 — лимита нет (значение по умолчанию в конфиге не должен ломать)."""
    unlimited = DNSCache(max_entries=0, janitor_interval=1.0)
    for i in range(50):
        unlimited.set(_request(f"x{i}.example"), False, _response(f"x{i}.example", ttl=60))
    assert len(unlimited) == 50


def test_prune_expired_counts_removed(cache, clock):
    """prune_expired() возвращает число удалённых записей."""
    for i in range(3):
        cache.set(_request(f"p{i}.example"), False, _response(f"p{i}.example", ttl=5))
    cache.set(_request("alive.example"), False, _response("alive.example", ttl=600))

    clock.advance(6)
    assert cache.prune_expired() == 3, "уборка насчитала не то число записей"
    assert cache.prune_expired() == 0, "повторная уборка нашла что-то ещё"
    assert cache.get_with_state(_request("alive.example"), False)[1] == "fresh"


def test_clear_removes_everything(cache):
    cache.set(_request(), False, _response(ttl=60))
    cache.clear()
    assert len(cache) == 0
    assert cache.get_with_state(_request(), False) == (None, "miss")


# ── Уборщик ─────────────────────────────────────────────────────────────────

def test_janitor_starts_once_and_stops(cache):
    """Второй start_janitor не создаёт второй поток, а stop останавливает уборщик."""
    cache.start_janitor()
    first = cache._janitor
    cache.start_janitor()
    assert cache._janitor is first, "запустился второй уборщик"

    cache.stop_janitor()
    assert cache._janitor is None or not cache._janitor.is_alive(), "уборщик не остановился"


def test_janitor_cleans_expired_entries(cache, clock, monkeypatch):
    """Уборщик действительно убирает: после его срабатывания мусор исчезает."""
    request = _request()
    cache.set(request, False, _response(ttl=5))
    clock.advance(6)

    # Не ждём реальный интервал: вызываем тело цикла через stop-событие.
    cache.start_janitor()
    try:
        for _ in range(50):
            if len(cache) == 0:
                break
            threading.Event().wait(0.01)
    finally:
        cache.stop_janitor()
    assert len(cache) == 0, "уборщик не удалил истёкшую запись"
