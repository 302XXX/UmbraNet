"""
UmbraNet — геолокация целей для Cyber-Map.
==========================================

Переводит «домен + IP, зарезолвленный через UmbraNet» в точку на карте
(широта/долгота + человекочитаемая метка).

Стратегия — три уровня, от быстрых к медленным:

  1. Встроенная офлайн-таблица популярных сетей (Google, Cloudflare, Meta,
     Telegram, Яндекс, VK, …). Работает без интернета, мгновенно.
     Честная оговорка: у anycast-сетей (Cloudflare, Google DNS) «место» —
     это ближайший узел, а не физический сервер; для радар-визуализации
     это нормально и честно помечено в подписи.

  2. Онлайн-запрос к бесплатному ip-api.com (без ключа, ~45 req/min).
     Выполняется в фоне (один daemon-воркер), результат кэшируется в памяти
     и на диске, а частота запросов ограничена лимитером — см. H5 ниже.
     Третьей стороне уходит IP посещённого сайта (не адрес пользователя).

  3. Ничего не нашли → локация неизвестна, луч не рисуем (только строка
     в журнале карты).

Модуль независим от GUI: может вызываться из любого потока. Колбэк
geolocate_async() вызывается в потоке-воркере — GUI обязан маршалить
результат в свой поток через сигнал.
"""

from __future__ import annotations

import atexit
import ipaddress
import json
import logging
import os
import queue
import threading
import time

import requests

log = logging.getLogger("UmbraNet.Geo")

# ── Гео-точка ────────────────────────────────────────────────────────────────
# (широта, долгота, подпись)

# Москва — «домашние» anycast-узлы: у российского пользователя трафик до
# Google DNS / Cloudflare обычно терминируется в MSK/Spb PoP.
_MSK = (55.75, 37.62)
_FRA = (50.11, 8.68)     # Франкфурт — крупнейший европейский хаб
_AMS = (52.37, 4.90)     # Амстердам
_ASH = (39.04, -77.49)   # Ашберн (Вирджиния, США)

# ── Уровень 1: встроенные сети ───────────────────────────────────────────────
# Формат: (сеть, (lat, lon, label))

_KNOWN: list[tuple] = [
    # Google (поиск, YouTube, googlevideo, Google Public DNS, GCP)
    ("8.8.8.0/24",        (_MSK[0], _MSK[1], "Google Public DNS (anycast)")),
    ("8.8.4.0/24",        (_MSK[0], _MSK[1], "Google Public DNS (anycast)")),
    ("142.250.0.0/15",    (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("172.217.0.0/16",    (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("216.58.192.0/19",   (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("74.125.0.0/16",     (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("173.194.0.0/16",    (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("209.85.128.0/17",   (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("64.233.160.0/19",   (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("66.102.0.0/20",     (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    ("2a00:1450::/32",    (_MSK[0], _MSK[1], "Google (ближайший узел)")),
    # Cloudflare (CDN, прокси большинства западных сервисов, 1.1.1.1)
    ("1.1.1.0/24",        (_MSK[0], _MSK[1], "Cloudflare DNS (anycast)")),
    ("1.0.0.0/24",        (_MSK[0], _MSK[1], "Cloudflare DNS (anycast)")),
    ("104.16.0.0/13",     (_MSK[0], _MSK[1], "Cloudflare (ближайший узел)")),
    ("172.64.0.0/13",     (_MSK[0], _MSK[1], "Cloudflare (ближайший узел)")),
    ("162.158.0.0/15",    (_MSK[0], _MSK[1], "Cloudflare (ближайший узел)")),
    ("188.114.96.0/20",   (_MSK[0], _MSK[1], "Cloudflare (ближайший узел)")),
    ("2606:4700::/32",    (_MSK[0], _MSK[1], "Cloudflare (ближайший узел)")),
    # Microsoft / Azure (в т.ч. OpenAI api через Azure Front Door)
    ("20.0.0.0/8",        (_AMS[0], _AMS[1], "Microsoft Azure (edge)")),
    ("13.64.0.0/11",      (_AMS[0], _AMS[1], "Microsoft Azure (edge)")),
    ("40.64.0.0/10",      (_AMS[0], _AMS[1], "Microsoft Azure (edge)")),
    ("104.40.0.0/13",     (_AMS[0], _AMS[1], "Microsoft Azure (edge)")),
    ("2a01:111::/32",     (_AMS[0], _AMS[1], "Microsoft Azure (edge)")),
    # Meta (Facebook / Instagram / WhatsApp)
    ("31.13.0.0/16",      (_FRA[0], _FRA[1], "Meta (edge)")),
    ("157.240.0.0/16",    (_FRA[0], _FRA[1], "Meta (edge)")),
    ("173.252.64.0/18",   (_FRA[0], _FRA[1], "Meta (edge)")),
    ("129.134.0.0/17",    (_FRA[0], _FRA[1], "Meta (edge)")),
    # Telegram
    ("149.154.160.0/20",  (_AMS[0], _AMS[1], "Telegram DC (Амстердам)")),
    ("91.108.0.0/16",     (_AMS[0], _AMS[1], "Telegram DC")),
    # GitHub
    ("140.82.112.0/20",   (_ASH[0], _ASH[1], "GitHub (Ашберн, США)")),
    # Amazon AWS (Netflix и куча всего)
    ("3.0.0.0/8",         (_FRA[0], _FRA[1], "Amazon AWS (edge)")),
    ("52.0.0.0/8",        (_FRA[0], _FRA[1], "Amazon AWS (edge)")),
    ("54.0.0.0/8",        (_FRA[0], _FRA[1], "Amazon AWS (edge)")),
    # Valve / Steam
    ("155.133.224.0/19",  (_FRA[0], _FRA[1], "Steam / Valve (edge)")),
    ("162.254.192.0/21",  (_FRA[0], _FRA[1], "Steam / Valve (edge)")),
    # Wikimedia
    ("198.35.26.0/23",    (_ASH[0], _ASH[1], "Wikimedia (США)")),
    # Яндекс
    ("77.88.0.0/18",      (_MSK[0], _MSK[1], "Яндекс (Москва)")),
    ("93.158.0.0/20",     (_MSK[0], _MSK[1], "Яндекс (Москва)")),
    ("5.45.192.0/18",     (_MSK[0], _MSK[1], "Яндекс (Москва)")),
    ("5.255.192.0/18",    (_MSK[0], _MSK[1], "Яндекс (Москва)")),
    ("2a02:6b8::/32",     (_MSK[0], _MSK[1], "Яндекс (Москва)")),
    # VK
    ("87.240.128.0/18",   (59.93, 30.33, "VK (Санкт-Петербург)")),
    ("93.186.224.0/20",   (59.93, 30.33, "VK (Санкт-Петербург)")),
    # Mail.ru
    ("217.69.128.0/20",   (_MSK[0], _MSK[1], "Mail.ru (Москва)")),
    ("94.100.176.0/20",   (_MSK[0], _MSK[1], "Mail.ru (Москва)")),
]

# ── Уровень 1b: домены, которые узнаём без IP (нет ответа / private IP) ──────
_KNOWN_DOMAINS: dict[str, tuple[float, float, str]] = {
    "openai.com":          (_ASH[0], _ASH[1], "OpenAI (США)"),
    "chatgpt.com":         (_ASH[0], _ASH[1], "OpenAI (США)"),
    "api.openai.com":      (_ASH[0], _ASH[1], "OpenAI API (США)"),
    "youtube.com":         (_MSK[0], _MSK[1], "YouTube (Google edge)"),
    "googlevideo.com":     (_MSK[0], _MSK[1], "YouTube video (Google edge)"),
    "discord.com":         (_MSK[0], _MSK[1], "Discord (Cloudflare edge)"),
    "github.com":          (_ASH[0], _ASH[1], "GitHub (США)"),
    "telegram.org":        (_AMS[0], _AMS[1], "Telegram (Амстердам)"),
    "wikipedia.org":       (_ASH[0], _ASH[1], "Wikipedia (США)"),
    "spotify.com":         (_FRA[0], _FRA[1], "Spotify (Франкфурт)"),
    "netflix.com":         (_FRA[0], _FRA[1], "Netflix (AWS edge)"),
    "steamcontent.com":    (_FRA[0], _FRA[1], "Steam CDN"),
    "yandex.ru":           (_MSK[0], _MSK[1], "Яндекс (Москва)"),
    "vk.com":              (59.93, 30.33, "VK (Санкт-Петербург)"),
    "mail.ru":             (_MSK[0], _MSK[1], "Mail.ru (Москва)"),
    "rutube.ru":           (_MSK[0], _MSK[1], "Rutube (Москва)"),
    "ozon.ru":             (_MSK[0], _MSK[1], "Ozon (Москва)"),
}

# Компилируем один раз при импорте: (ip_network, (lat, lon, label))
_KNOWN_NETS: list[tuple] = []
for _cidr, _geo in _KNOWN:
    try:
        _KNOWN_NETS.append((ipaddress.ip_network(_cidr), _geo))
    except ValueError:
        log.debug("bad builtin network: %s", _cidr)

# ── Уровень 2: онлайн-геолокация ip-api.com ─────────────────────────────────
#
# H5: лимитер, очередь и дисковый кэш.
#
# Бесплатный тариф ip-api.com — около 45 запросов в минуту с одного адреса.
# Раньше ограничений не было вообще: каждый новый IP из карты сразу уходил в
# сеть, отдельным потоком. При активном сёрфинге сервис отвечает 429 (бан), и
# карта остаётся без геолокации — причём навсегда: неудачный ответ («None»)
# тоже попадал в кэш и больше не перепроверялся. Теперь:
#
#   • ровный шаг: не чаще одного запроса в _MIN_INTERVAL_S секунд и не больше
#     _RATE_LIMIT_PER_MIN в минуту (лимит сервиса 45 — держим 40 с запасом);
#   • один воркер: онлайн-запросы идут по очереди из одного потока, а не
#     «поток на каждый IP» — иначе лимитер бесполезен, запросы уходят пачкой;
#   • пауза после 429: заметив бан, ждём _BAN_COOLDOWN_S и только потом пробуем;
#   • у кэша есть сроки: найденное живёт _POSITIVE_TTL_S, неудача — 
#     _NEGATIVE_TTL_S (бан/обрыв не «залипают» навсегда);
#   • у кэша есть предел (_CACHE_MAX): сперва выкидываем протухшее, потом самое
#     старое, чтобы память не росла без границы;
#   • найденное сохраняется на диск (geo_cache.json) — после перезапуска
#     сервис не опрашивается заново по тем же IP;
#   • если сервис не ответил, а прежнее значение в кэше было — отдаём прежнее,
#     чтобы точка на карте не пропадала из-за временного сбоя.
#
# Лимитер рассчитан на «пульс» самой карты: лучи рисуются не чаще, чем раз в
# MIN_BEAM_INTERVAL (1.2 с, см. Map/core/map_dialog.py), то есть максимум ~50
# запросов в минуту — предел 40 держит нас ниже лимита сервиса.

_API_URL = "http://ip-api.com/json/{ip}"      # бесплатный тариф — только http
_API_FIELDS = "status,message,country,city,lat,lon"
_API_TIMEOUT = 4.0

_RATE_LIMIT_PER_MIN = 40        # лимит сервиса 45/мин, берём с запасом
_MIN_INTERVAL_S = 1.5           # ровный шаг: 40 запросов в минуту
_BAN_COOLDOWN_S = 60.0          # пауза после ответа 429/403
_POSITIVE_TTL_S = 24 * 60 * 60  # найденная гео-точка: сутки
_NEGATIVE_TTL_S = 10 * 60       # неудача: пробуем снова через 10 минут
_CACHE_MAX = 512                # записей в памяти
_QUEUE_MAX = 256                # запросов в очереди
_QUEUE_TTL_S = 5.0              # столько ждём своей очереди; позже луч уже неактуален
_DISK_SAVE_MIN_INTERVAL_S = 30.0
_DISK_SAVE_EVERY_N = 8          # столько новых записей — уже повод записать файл

_CACHE_FILENAME = "geo_cache.json"

# Часы и пауза вынесены в переменные модуля: тесты подменяют их виртуальными,
# чтобы проверять лимитер без реальных ожиданий.
_now_wall = time.time           # настенные часы: сроки годности (нужны на диске)
_now_mono = time.monotonic      # монотонные часы: шаг лимитера
_sleep = time.sleep             # пауза лимитера

_cache: dict[str, tuple] = {}   # ip -> (значение (lat, lon, label) | None, годен до)
_cache_lock = threading.Lock()

_inflight: set[str] = set()     # что уже стоит в очереди или считается
_inflight_lock = threading.Lock()

_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker_started = False
_worker_lock = threading.Lock()

_stats: dict[str, int] = {"запросов": 0, "из_кэша": 0, "отброшено": 0, "банов": 0}
_stats_lock = threading.Lock()

_disk_path_override: str | None = None
_disk_loaded = False
_disk_lock = threading.Lock()
_last_disk_save = 0.0
_unsaved = 0                    # новых записей с прошлого сохранения

_ENV_CACHE_PATH = "UMBRANET_GEO_CACHE"


class _RateLimiter:
    """Не больше `per_minute` запросов в минуту, ровным шагом, с паузой после бана.

    Работает на монотонных часах (`_now_mono`), поэтому перевод системного
    времени или сон машины не сбивают окно. `acquire()` блокирующий: его зовут
    только из воркера, GUI сюда не попадает.
    """

    def __init__(self, per_minute: int = _RATE_LIMIT_PER_MIN,
                 min_interval: float = _MIN_INTERVAL_S) -> None:
        self._per_minute = max(1, int(per_minute))
        self._min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._sent: list[float] = []          # отметки отправок за последнюю минуту
        self._next_at = 0.0                   # раньше этого момента не отправляем
        self._blocked_until = 0.0             # пауза после бана

    def _delay(self) -> float:
        """Сколько ждать; если можно отправлять — отмечает отправку и вернёт 0."""
        with self._lock:
            now = _now_mono()
            self._sent = [t for t in self._sent if now - t < 60.0]
            delay = 0.0
            if now < self._blocked_until:
                delay = self._blocked_until - now
            elif len(self._sent) >= self._per_minute:
                delay = 60.0 - (now - self._sent[0])
            elif now < self._next_at:
                delay = self._next_at - now
            if delay <= 0.0:
                self._sent.append(now)
                self._next_at = now + self._min_interval
                return 0.0
            return float(delay)

    def acquire(self) -> None:
        """Ждёт разрешения на запрос (блокирующе, из воркера)."""
        while True:
            delay = self._delay()
            if delay <= 0.0:
                return
            _sleep(delay)

    def note_banned(self, seconds: float = _BAN_COOLDOWN_S) -> None:
        """Сервис ответил 429/403 — держим паузу, иначе бан продлится."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, _now_mono() + float(seconds))

    def state(self) -> dict:
        """Короткая сводка для лога: сколько в окне, сколько ждать."""
        with self._lock:
            now = _now_mono()
            in_window = len([t for t in self._sent if now - t < 60.0])
            return {
                "в_окне": in_window,
                "предел": self._per_minute,
                "пауза_с": round(max(0.0, self._blocked_until - now), 1),
                "шаг_с": round(max(0.0, self._next_at - now), 1),
            }


_limiter = _RateLimiter()


def _lookup_builtin(ip_str: str) -> tuple | None:
    """Офлайн-таблица. Возвращает (lat, lon, label) или None."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    if not addr.is_global:
        return None
    for net, geo in _KNOWN_NETS:
        if addr in net:
            return geo
    return None


def _transport(url: str, params: dict, timeout: float):
    """HTTP-запрос к ip-api. Отдельная функция — её подменяют тесты."""
    return requests.get(url, params=params, timeout=timeout)


def _lookup_online_sync(ip_str: str) -> tuple | None:
    """Синхронный запрос к ip-api.com. Вызывать только из воркер-потока."""
    with _stats_lock:
        _stats["запросов"] += 1
    try:
        resp = _transport(_API_URL.format(ip=ip_str),
                          {"fields": _API_FIELDS}, _API_TIMEOUT)
    except Exception as exc:                      # транспорт может упасть по-разному
        log.debug("ip-api lookup failed for %s: %s", ip_str, exc)
        return None

    code = getattr(resp, "status_code", None)
    if code in (429, 403):
        # Бан или исчерпан лимит: без паузы следующий запрос только продлит его.
        _limiter.note_banned()
        with _stats_lock:
            _stats["банов"] += 1
        log.warning("ip-api вернул %s — пауза %.0f с (лимитер: %s)",
                    code, _BAN_COOLDOWN_S, _limiter.state())
        return None

    try:
        data = resp.json()
    except (TypeError, ValueError) as exc:
        log.debug("ip-api вернул не-JSON для %s: %s", ip_str, exc)
        return None
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    city = str(data.get("city") or "").strip()
    country = str(data.get("country") or "").strip()
    label = ", ".join(p for p in (city, country) if p) or "IP-геолокация"
    return (lat, lon, label)


# ── Кэш в памяти ────────────────────────────────────────────────────────────

def _cache_get(ip_str: str) -> tuple[bool, tuple | None, bool]:
    """(нашли ли, значение, свежее ли).

    «Нашли, но не свежее» — повод пойти в сеть заново, а прежнее значение
    остаётся как запасное: если сети нет, карта покажет хотя бы старое.
    """
    with _cache_lock:
        item = _cache.get(ip_str)
    if item is None:
        return False, None, False
    value, expires_at = item
    return True, value, expires_at > _now_wall()


def _evict_locked() -> None:
    """Держим кэш в пределах _CACHE_MAX: сперва протухшее, потом самое старое."""
    now = _now_wall()
    for key in [k for k, (_v, exp) in _cache.items() if exp <= now]:
        _cache.pop(key, None)
    while len(_cache) > _CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)


def _cache_put(ip_str: str, value: tuple | None, ttl: float | None = None) -> None:
    """Кладёт результат в кэш. Найденное живёт долго, неудача — недолго."""
    global _unsaved
    if ttl is None:
        ttl = _POSITIVE_TTL_S if value is not None else _NEGATIVE_TTL_S
    with _cache_lock:
        is_new = ip_str not in _cache
        _cache[ip_str] = (value, _now_wall() + float(ttl))
        if is_new:
            _unsaved += 1
        if len(_cache) > _CACHE_MAX:
            _evict_locked()


# ── Кэш на диске ────────────────────────────────────────────────────────────

def cache_path() -> str:
    """Абсолютный путь к дисковому кэшу гео-точек."""
    if _disk_path_override:
        return _disk_path_override
    env = os.environ.get(_ENV_CACHE_PATH)
    if env:
        return os.path.abspath(env)
    # Map/core/geo.py → Map/core → Map → корень программы
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, _CACHE_FILENAME)


def set_cache_path(path: str | None) -> str:
    """Подменить файл кэша (нужно тестам: пишем в tmp, а не в проект)."""
    global _disk_path_override, _disk_loaded
    _disk_path_override = os.path.abspath(path) if path else None
    _disk_loaded = False
    return cache_path()


def _load_disk() -> None:
    """Читает дисковый кэш один раз за запуск. Битый файл — просто игнорируем."""
    global _disk_loaded
    with _disk_lock:
        if _disk_loaded:
            return
        _disk_loaded = True
    path = cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:          # нет прав, обрезанный/битый JSON
        log.debug("Дисковый кэш гео не прочитан (%s): %s", path, exc)
        return
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return
    now = _now_wall()
    loaded = 0
    with _cache_lock:
        for key, item in entries.items():
            if not isinstance(item, list) or len(item) != 2:
                continue
            value, expires_at = item
            try:
                expires_at = float(expires_at)
            except (TypeError, ValueError):
                continue
            if expires_at <= now:
                continue
            if value is not None:
                if not (isinstance(value, (list, tuple)) and len(value) == 3):
                    continue
                value = tuple(value)
            _cache[str(key)] = (value, expires_at)
            loaded += 1
        if len(_cache) > _CACHE_MAX:
            _evict_locked()
    log.debug("Дисковый кэш гео: загружено %d записей", loaded)


def _save_disk(force: bool = False) -> None:
    """Сохраняет кэш на диск (атомарно).

    Не чаще раза в _DISK_SAVE_MIN_INTERVAL_S секунд, НО и не реже, чем каждые
    _DISK_SAVE_EVERY_N новых записей: иначе заглянувший на минуту пользователь
    получил бы на диске одну-две точки, а остальное терялось бы при закрытии.
    """
    global _last_disk_save, _unsaved
    with _disk_lock:
        now = _now_wall()
        by_time = (now - _last_disk_save) >= _DISK_SAVE_MIN_INTERVAL_S
        if not force and not by_time and _unsaved < _DISK_SAVE_EVERY_N:
            return
        _last_disk_save = now
        _unsaved = 0
        with _cache_lock:
            entries = {k: [list(v) if v is not None else None, exp]
                       for k, (v, exp) in _cache.items()}
    if not entries:
        # Пустой снимок писать нечего — и нельзя: запись может случиться в момент,
        # когда кэш уже сброшен (например, тестом), и тогда в рабочей папке
        # остался бы пустой geo_cache.json. Проверено на практике.
        return
    path = cache_path()
    try:
        payload = {"_saved_at": _now_wall(), "entries": entries}
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)          # атомарная замена: файл не бывает битым
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except (OSError, TypeError, ValueError) as exc:
        log.debug("Дисковый кэш гео не сохранён (%s): %s", path, exc)


# ── Воркер: один поток на все онлайн-запросы ────────────────────────────────

def _notify(callback, value: tuple | None) -> None:
    if callback is None or value is None:
        return
    try:
        callback(*value)
    except Exception as exc:
        log.debug("Колбэк геолокации упал: %s", exc)


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker_loop, name="geo-worker", daemon=True).start()
        _worker_started = True


def _worker_loop() -> None:
    """Разбирает очередь: кэш → офлайн-таблица → (с паузой лимитера) ip-api."""
    while True:
        ip_str, callback, queued_at = _queue.get()
        try:
            with _inflight_lock:
                _inflight.discard(ip_str)

            if (_now_mono() - queued_at) > _QUEUE_TTL_S:
                # Пока ждали своей очереди, луч на карте уже погас — не тратим
                # запрос: лимит дороже, чем точка на протухшем кадре.
                with _stats_lock:
                    _stats["отброшено"] += 1
                continue

            found, value, fresh = _cache_get(ip_str)
            if found and fresh:
                with _stats_lock:
                    _stats["из_кэша"] += 1
                _notify(callback, value)
                continue

            builtin = _lookup_builtin(ip_str)
            if builtin is not None:
                _cache_put(ip_str, builtin)
                _notify(callback, builtin)
                continue

            _limiter.acquire()
            result = _lookup_online_sync(ip_str)
            _cache_put(ip_str, result)
            _save_disk()
            if result is not None:
                _notify(callback, result)
            elif found:
                # Сбой (нет сети, бан) — отдаём прежнее значение, чтобы точка
                # на карте не пропала из-за временной неудачи.
                _notify(callback, value)
        except Exception as exc:
            log.debug("geo-worker: %s", exc)
        finally:
            _queue.task_done()


# ── Публичный интерфейс ─────────────────────────────────────────────────────

def geolocate_sync(ip_str: str) -> tuple | None:
    """Кэш → офлайн-таблица → ip-api.com (синхронно, может блокировать!).

    Учитывает лимитер: при исчерпанной минуте запрос ЖДЁТ своей очереди.
    Вызывать только из фонового потока, не из GUI.
    """
    if not ip_str:
        return None
    ip_str = ip_str.strip()
    _load_disk()

    found, value, fresh = _cache_get(ip_str)
    if found and fresh:
        return value
    builtin = _lookup_builtin(ip_str)
    if builtin is not None:
        _cache_put(ip_str, builtin)
        return builtin

    _limiter.acquire()
    result = _lookup_online_sync(ip_str)
    _cache_put(ip_str, result)
    _save_disk()
    if result is None and found:
        return value                     # запасное значение лучше пустоты
    return result


def geolocate_async(ip_str: str, callback) -> None:
    """Неблокирующая геолокация: ответ придёт в callback(lat, lon, label).

    Мгновенный путь (кэш, офлайн-таблица) вызывает callback прямо здесь, в
    потоке вызывающего; сетевой запрос ставится в очередь и выполняется в
    одном воркере с соблюдением лимита частоты. Если такой IP уже в очереди,
    callback НЕ вызывается вовсе (вызывающий сам решает, что делать).
    Никогда не бросает исключений наружу.
    """
    if not callable(callback):
        return
    ip_str = (ip_str or "").strip()
    if not ip_str:
        return

    _load_disk()

    # Мгновенный путь: свежий кэш или офлайн-таблица — без очереди и без сети.
    found, value, fresh = _cache_get(ip_str)
    if found and fresh:
        with _stats_lock:
            _stats["из_кэша"] += 1
        _notify(callback, value)
        return
    builtin = _lookup_builtin(ip_str)
    if builtin is not None:
        _cache_put(ip_str, builtin)
        _notify(callback, builtin)
        return

    # Онлайн: не плодим дублирующие запросы на один и тот же IP.
    with _inflight_lock:
        if ip_str in _inflight:
            return
        _inflight.add(ip_str)

    try:
        _queue.put_nowait((ip_str, callback, _now_mono()))
    except queue.Full:
        with _inflight_lock:
            _inflight.discard(ip_str)
        with _stats_lock:
            _stats["отброшено"] += 1
        log.debug("Очередь геолокации переполнена (%d), %s пропущен", _QUEUE_MAX, ip_str)
        return

    _ensure_worker()


def stats() -> dict:
    """Сводка для лога и тестов: лимитер, кэш, счётчики."""
    with _stats_lock:
        counters = dict(_stats)
    with _cache_lock:
        size = len(_cache)
    return {"лимитер": _limiter.state(), "кэш": size, **counters}


def _flush_on_exit() -> None:
    """Записать кэш при выходе программы: то, что не успело сохраниться по таймеру.

    Без этого последние найденные точки терялись бы вместе с процессом — и при
    следующем запуске карта снова спрашивала бы ip-api про те же IP.

    Если записывать нечего — файла не создаём. Мелочь, но важная: при выходе из
    тестов, где путь кэша подменён на временный, обработчик создавал бы пустой
    `geo_cache.json` в корне проекта.
    """
    with _cache_lock:
        if not _cache:
            return
    try:
        _save_disk(force=True)
    except Exception as exc:                       # выход не должен падать из-за кэша
        log.debug("Кэш гео не записан при выходе: %s", exc)


atexit.register(_flush_on_exit)


def reset_state(clear_disk: bool = False) -> None:
    """Сбрасывает кэш и счётчики (тесты; смена профиля).

    `clear_disk=True` заодно удаляет файл кэша — иначе следующий запуск
    поднимет старые записи обратно.
    """
    global _last_disk_save, _disk_loaded, _unsaved
    with _cache_lock:
        _cache.clear()
    with _inflight_lock:
        _inflight.clear()
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0
    # Сброс внутренностей лимитера: тесты не должны наследовать окно от прошлого теста.
    _limiter._sent.clear()
    _limiter._next_at = 0.0
    _limiter._blocked_until = 0.0
    _last_disk_save = 0.0
    _unsaved = 0
    _disk_loaded = False
    if clear_disk:
        try:
            os.remove(cache_path())
        except OSError:
            pass


def domain_fallback(domain: str) -> tuple | None:
    """Гео по имени домена, когда IP нет/не геолокируется.

    Сравниваем по суффиксам («cdn.oaistatic.com» содержит «oaistatic.com» —
    не найдём, а вот «api.openai.com» найдётся точным суффиксом).
    """
    d = str(domain or "").strip().lower().rstrip(".")
    if not d:
        return None
    best = None
    for known, geo in _KNOWN_DOMAINS.items():
        if d == known or d.endswith("." + known):
            if best is None or len(known) > len(best[0]):
                best = (known, geo)
    return best[1] if best else None


def is_public_ip(value: str) -> bool:
    """True, если строка — корректный глобальный IPv4/IPv6."""
    try:
        addr = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    return addr.is_global and not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
    )
