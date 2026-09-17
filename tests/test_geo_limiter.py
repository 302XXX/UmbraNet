"""
Тесты геолокации карты: лимитер ip-api, очередь и кэш (H5).
==========================================================

Что было сломано. Уровень 2 геолокации (`Map/core/geo.py`) обращался к
бесплатному ip-api.com вообще без ограничений: каждый новый IP из карты
порождал отдельный поток и немедленно уходил в сеть. Бесплатный тариф — около
45 запросов в минуту, поэтому при активном сёрфинге сервис отвечал 429 (бан).
Оставались две беды:

  1. бан навсегда: неудачный ответ («None») тоже кэшировался — и тоже навсегда,
     так что IP больше никогда не проверялся, даже когда бан снялся;
  2. поток на каждый запрос: лимитер без единого воркера бессмысленен — запросы
     уходят пачкой одновременно.

Что проверяем: ровный шаг и потолок запросов в минуту, пауза после 429, один
воркер (в сети не больше одного запроса), сроки годности у успеха и у неудачи,
предел размера кэша, дисковый кэш (переживает «перезапуск» и терпит битый файл),
офлайн-таблица не тратит лимит вовсе.

Часы и HTTP-транспорт подменяются: тесты идут мгновенно и без сети.

Запуск: python -m pytest tests/test_geo_limiter.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Map.core import geo

# Публичные IP, которых нет ни в офлайн-таблице, ни в приватных диапазонах.
_FREE_IP = "45.33.32.156"
_FREE_IP2 = "45.33.32.157"
_FREE_IP3 = "45.33.32.158"


class _Clock:
    """Виртуальные часы: и монотонные, и настенные — чтобы тесты не ждали."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = float(start)

    def mono(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class _Resp:
    """Минимальная заглушка ответа requests."""

    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _ok(lat: float = 52.37, lon: float = 4.90, city: str = "Amsterdam",
        country: str = "Netherlands") -> _Resp:
    return _Resp({"status": "success", "country": country, "city": city,
                  "lat": lat, "lon": lon})


class _Net:
    """Заглушка сети: считает запросы и максимальную одновременность."""

    def __init__(self, responder=None, delay: float = 0.0) -> None:
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.live = 0
        self.max_live = 0
        self.responder = responder or (lambda ip: _ok())
        self.delay = delay

    def __call__(self, url: str, params: dict, timeout: float):
        with self.lock:
            self.calls.append(url)
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            if self.delay:
                time.sleep(self.delay)
            ip = url.rsplit("/", 1)[-1]
            result = self.responder(ip)
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            with self.lock:
                self.live -= 1

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.calls)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Изолированное окружение гео: свой файл кэша, виртуальные часы, заглушка сети."""
    clock = _Clock()
    net = _Net()
    monkeypatch.setattr(geo, "_now_mono", clock.mono)
    monkeypatch.setattr(geo, "_now_wall", clock.wall)
    monkeypatch.setattr(geo, "_sleep", clock.sleep)
    monkeypatch.setattr(geo, "_transport", net)
    geo.set_cache_path(str(tmp_path / "geo_cache.json"))
    geo.reset_state()
    yield {"clock": clock, "net": net, "cache": tmp_path / "geo_cache.json"}
    geo.set_cache_path(None)
    geo.reset_state()


def _drain_queue(timeout: float = 2.0) -> None:
    """Ждём, пока воркер разберёт очередь (тесты идут на виртуальных часах)."""
    deadline = time.monotonic() + timeout
    while geo._queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.005)


# ── Лимитер ─────────────────────────────────────────────────────────────────

def test_limiter_keeps_even_step_between_requests(env):
    """Запросы идут ровным шагом: между соседними — не меньше шага лимитера."""
    clock = env["clock"]
    limiter = geo._RateLimiter(per_minute=40, min_interval=1.5)

    stamps = []
    for _ in range(3):
        limiter.acquire()
        stamps.append(clock.mono())

    assert stamps[1] - stamps[0] >= 1.5 - 1e-6, f"шаг между запросами {stamps[1] - stamps[0]}"
    assert stamps[2] - stamps[1] >= 1.5 - 1e-6
    assert limiter.state()["в_окне"] == 3


def test_limiter_respects_minute_cap(env):
    """В минуту уходит не больше предела: лишний запрос ждёт, пока окно сдвинется."""
    clock = env["clock"]
    limiter = geo._RateLimiter(per_minute=40, min_interval=0.0)

    for _ in range(40):
        limiter.acquire()
    after_cap = clock.mono()

    limiter.acquire()                     # 41-й: должен ждать освобождения окна
    assert clock.mono() - after_cap >= 60.0 - 1e-6, "41-й запрос ушёл, не дождавшись окна"
    assert len(limiter._sent) <= 41


def test_limiter_pauses_after_ban(env):
    """После ответа 429 лимитер держит паузу, а не бьётся в закрытую дверь."""
    clock = env["clock"]
    limiter = geo._RateLimiter()
    limiter.note_banned(60.0)

    started = clock.mono()
    limiter.acquire()
    assert clock.mono() - started >= 60.0 - 1e-6, "запрос ушёл сразу после бана"
    assert limiter.state()["пауза_с"] == 0.0


# ── Сеть и кэш ──────────────────────────────────────────────────────────────

def test_builtin_ip_does_not_spend_quota(env):
    """IP из офлайн-таблицы отвечает мгновенно и не тратит лимит вовсе."""
    net, clock = env["net"], env["clock"]
    started = clock.mono()

    result = geo.geolocate_sync("8.8.8.8")

    assert result is not None and "Google" in result[2], f"офлайн-таблица не сработала: {result}"
    assert net.count == 0, "запрос ушёл в сеть, хотя IP есть в офлайн-таблице"
    assert clock.mono() == started, "офлайн-ответ почему-то ждал лимитер"
    assert geo.stats()["запросов"] == 0


def test_result_cached_and_not_requested_twice(env):
    """Найденный IP запрашивается один раз: дальше ответ берётся из кэша."""
    net = env["net"]

    first = geo.geolocate_sync(_FREE_IP)
    second = geo.geolocate_sync(_FREE_IP)

    assert first == second == (52.37, 4.90, "Amsterdam, Netherlands")
    assert net.count == 1, f"в сеть ушло {net.count} запросов вместо одного"


def test_negative_result_expires_and_is_retried(env):
    """Неудача кэшируется, но не навсегда: через срок годности IP проверяется снова."""
    clock, net = env["clock"], env["net"]
    net.responder = lambda ip: _Resp({"status": "fail", "message": "reserved range"})

    assert geo.geolocate_sync(_FREE_IP) is None
    assert net.count == 1
    geo.geolocate_sync(_FREE_IP)
    assert net.count == 1, "неудача не закэшировалась — лимит тратится на каждый вызов"

    clock.advance(geo._NEGATIVE_TTL_S + 1)
    net.responder = lambda ip: _ok()
    result = geo.geolocate_sync(_FREE_IP)

    assert net.count == 2, "после истечения срока годности запрос не повторился"
    assert result is not None, "повторный запрос не дал гео-точку"


def test_ban_pauses_and_does_not_lose_old_value(env):
    """Бан (429) ставит паузу и не стирает прежнюю гео-точку из кэша."""
    clock, net = env["clock"], env["net"]
    net.responder = lambda ip: _ok(55.75, 37.62, "Moscow", "Russia")
    first = geo.geolocate_sync(_FREE_IP)
    assert first is not None

    clock.advance(geo._POSITIVE_TTL_S + 1)      # точка «протухла» — идём обновлять
    net.responder = lambda ip: _Resp({"status": "fail"}, status_code=429)

    result = geo.geolocate_sync(_FREE_IP)

    assert result == first, "при бане потерялось прежнее значение гео-точки"
    assert geo.stats()["банов"] == 1
    assert geo.stats()["лимитер"]["пауза_с"] > 0, "после 429 не выставлена пауза"


def test_http_error_is_not_fatal(env):
    """Сетевая ошибка не роняет карту: возвращаем None и живём дальше."""
    env["net"].responder = lambda ip: RuntimeError("нет сети")
    assert geo.geolocate_sync(_FREE_IP) is None
    assert geo.geolocate_sync(_FREE_IP2) is None


def test_cache_has_size_limit(env):
    """Кэш не растёт без границы: лишние записи вытесняются."""
    clock = env["clock"]
    for i in range(geo._CACHE_MAX + 100):
        geo._cache_put(f"198.51.{i // 250}.{i % 250}", (1.0, 2.0, "x"))

    assert len(geo._cache) <= geo._CACHE_MAX, f"кэш вырос до {len(geo._cache)}"

    # Протухшие записи уходят первыми: старые точки не занимают место вечно.
    clock.advance(geo._POSITIVE_TTL_S + 1)
    geo._cache_put("203.0.113.0", None)
    assert len(geo._cache) < geo._CACHE_MAX, "протухшие записи не вытесняются"


# ── Диск ────────────────────────────────────────────────────────────────────

def test_disk_cache_survives_restart(env):
    """Найденное сохраняется на диск: после «перезапуска» сеть не дёргается."""
    net, cache_file = env["net"], env["cache"]

    assert geo.geolocate_sync(_FREE_IP) is not None
    geo._save_disk(force=True)
    assert cache_file.exists(), "дисковый кэш не создан"

    geo.reset_state()                                # «перезапуск»: память пуста
    net.responder = lambda ip: (_ for _ in ()).throw(AssertionError("сеть не должна зваться"))
    result = geo.geolocate_sync(_FREE_IP)

    assert result == (52.37, 4.90, "Amsterdam, Netherlands"), f"из диска пришло {result}"


def test_disk_cache_saved_as_entries_accumulate(env):
    """Кэш уходит на диск и по количеству: заглянул на минуту — точки не потерялись."""
    net, cache_file = env["net"], env["cache"]
    net.responder = lambda ip: _ok(52.37, 4.90, "Amsterdam")

    # Первая точка сохраняется сразу, дальше — каждые _DISK_SAVE_EVERY_N новых.
    count = geo._DISK_SAVE_EVERY_N + 2
    for i in range(count):
        geo.geolocate_sync(f"45.33.{i // 250}.{i % 250}")

    saved = json.loads(cache_file.read_text(encoding="utf-8"))["entries"]
    assert len(saved) >= geo._DISK_SAVE_EVERY_N, (
        f"на диске только {len(saved)} точек из {count} найденных — остальные потеряны"
    )


def test_empty_cache_does_not_create_file(env):
    """Пустой кэш при выходе не создаёт файл: иначе в проекте появлялся бы мусор.

    Так было на практике: обработчик выхода писал кэш даже когда писать было
    нечего, и после прогона тестов в корне проекта оставался пустой
    `geo_cache.json`.
    """
    cache_file = env["cache"]
    assert not cache_file.exists()

    geo._flush_on_exit()
    geo._save_disk(force=True)          # и явная запись тоже ничего не создаёт

    assert not cache_file.exists(), "при выходе создан пустой файл кэша"


def test_broken_disk_cache_is_ignored(env):
    """Битый файл кэша не ломает геолокацию — просто начинаем с чистого листа."""
    cache_file = env["cache"]
    cache_file.write_text("{это не json", encoding="utf-8")

    result = geo.geolocate_sync(_FREE_IP)

    assert result is not None, "битый кэш помешал обычной работе"
    assert env["net"].count == 1


def test_expired_disk_entries_are_dropped(env):
    """Просроченные записи с диска не поднимаются: карта не показывает старьё вечно."""
    cache_file = env["cache"]
    cache_file.write_text(json.dumps({
        "_saved_at": 0,
        "entries": {_FREE_IP: [[1.0, 2.0, "старое"], env["clock"].wall() - 10]},
    }), encoding="utf-8")

    geo.reset_state()                                 # заставим перечитать файл
    result = geo.geolocate_sync(_FREE_IP)

    assert result != (1.0, 2.0, "старое"), "просроченная запись с диска всё ещё используется"
    assert result is not None


# ── Асинхронный путь ────────────────────────────────────────────────────────

def test_async_instant_path_calls_back_without_network(env):
    """Кэш и офлайн-таблица отвечают сразу, в потоке вызывающего, без сети."""
    net = env["net"]
    got = []
    geo.geolocate_sync("8.8.8.8")                     # наполняем кэш офлайн-ответом

    geo.geolocate_async("8.8.8.8", lambda *a: got.append(a))

    assert len(got) == 1, "мгновенный путь не вызвал колбэк"
    assert net.count == 0


def test_async_uses_single_worker(env):
    """Онлайн-запросы идут через ОДИН воркер: в сети одновременно не больше одного."""
    net = env["net"]
    net.delay = 0.05          # без задержки параллельные запросы просто не пересекутся
    done = threading.Event()
    got = []

    def cb(lat, lon, label):
        got.append((lat, lon, label))
        if len(got) == 3:
            done.set()

    for ip in (_FREE_IP, _FREE_IP2, _FREE_IP3):
        geo.geolocate_async(ip, cb)

    assert done.wait(3.0), f"не дождались гео для трёх IP (получено {len(got)})"
    assert net.count == 3
    assert net.max_live == 1, f"запросы шли параллельно ({net.max_live} сразу) — это не лимитер"
    workers = [t for t in threading.enumerate() if t.name == "geo-worker"]
    assert len(workers) == 1, f"воркеров геолокации {len(workers)} — должен быть ровно один"


def test_async_does_not_duplicate_inflight(env):
    """Повторный вызов для того же IP, пока он в очереди, новый запрос не создаёт."""
    net = env["net"]
    net.delay = 0.05                                  # успеваем позвать второй раз
    got = []
    geo.geolocate_async(_FREE_IP, lambda *a: got.append(a))
    geo.geolocate_async(_FREE_IP, lambda *a: got.append(a))

    _drain_queue()

    assert net.count == 1, f"на один IP ушло {net.count} запросов"
    assert len(got) == 1, "гео-точка пришла дважды"


def test_async_keeps_old_value_when_refresh_fails(env):
    """Асинхронный путь (его использует карта) отдаёт прежнюю точку, если обновление сорвалось.

    Сценарий: гео-точка была, срок годности истёк, пришёл новый запрос — а сервис
    ответил 429. Луч должен быть нарисован по ПРЕЖНЕЙ точке, а не пропасть.
    """
    clock, net = env["clock"], env["net"]
    net.responder = lambda ip: _ok(55.75, 37.62, "Moscow", "Russia")
    geo.geolocate_sync(_FREE_IP)                       # наполняем кэш
    clock.advance(geo._POSITIVE_TTL_S + 1)             # точка «протухла»

    net.responder = lambda ip: _Resp({"status": "fail"}, status_code=429)
    got = []
    done = threading.Event()

    def cb(lat, lon, label):
        got.append((lat, lon, label))
        done.set()

    geo.geolocate_async(_FREE_IP, cb)

    assert done.wait(3.0), "колбэк не позвали — точка на карте пропала из-за сбоя сервиса"
    assert got == [(55.75, 37.62, "Moscow, Russia")], f"пришло {got}, а не прежняя точка"


def test_stale_queue_item_is_dropped(env):
    """Залежавшийся в очереди IP не тратит лимит: к моменту ответа луч уже погас."""
    net = env["net"]
    got = []
    stale_at = geo._now_mono() - geo._QUEUE_TTL_S - 1

    geo._queue.put_nowait((_FREE_IP, lambda *a: got.append(a), stale_at))
    _drain_queue()

    assert net.count == 0, "просроченный запрос всё равно ушёл в сеть"
    assert got == []
    assert geo.stats()["отброшено"] >= 1
