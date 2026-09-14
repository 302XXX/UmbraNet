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
     Выполняется в фоне (daemon-поток), результат кэшируется в памяти.
     Третьей стороне уходит IP посещённого сайта (не адрес пользователя).

  3. Ничего не нашли → локация неизвестна, луч не рисуем (только строка
     в журнале карты).

Модуль независим от GUI: может вызываться из любого потока. Колбэк
geolocate_async() вызывается в потоке-воркере — GUI обязан маршалить
результат в свой поток через сигнал.
"""

from __future__ import annotations

import ipaddress
import logging
import threading

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

_API_URL = "http://ip-api.com/json/{ip}"
_API_FIELDS = "status,message,country,city,lat,lon"
_API_TIMEOUT = 4.0

_cache: dict[str, tuple | None] = {}          # ip -> (lat, lon, label) | None
_cache_lock = threading.Lock()
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


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


def _lookup_online_sync(ip_str: str) -> tuple | None:
    """Синхронный запрос к ip-api.com. Вызывать только из воркер-потока."""
    try:
        resp = requests.get(
            _API_URL.format(ip=ip_str),
            params={"fields": _API_FIELDS},
            timeout=_API_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:
        log.debug("ip-api lookup failed for %s: %s", ip_str, exc)
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


def geolocate_sync(ip_str: str) -> tuple | None:
    """Кэш → офлайн-таблица → ip-api.com (синхронно, может блокировать!)."""
    if not ip_str:
        return None
    ip_str = ip_str.strip()
    with _cache_lock:
        if ip_str in _cache:
            return _cache[ip_str]

    result = _lookup_builtin(ip_str) or _lookup_online_sync(ip_str)

    with _cache_lock:
        _cache[ip_str] = result
    return result


def geolocate_async(ip_str: str, callback) -> None:
    """Неблокирующая геолокация: ответ придёт в callback(lat, lon, label).

    callback вызывается в потоке-воркере (daemon). Если IP пустой или уже
    в работе — callback НЕ вызывается вовсе (вызывающий сам решает, что
    делать). Никогда не бросает исключений наружу.
    """
    if not callable(callback):
        return
    ip_str = (ip_str or "").strip()
    if not ip_str:
        return

    # Мгновенный путь: кэш или офлайн-таблица — без потока.
    with _cache_lock:
        cached = _cache.get(ip_str)
    if cached is not None:
        try:
            callback(*cached)
        except Exception:
            pass
        return
    builtin = _lookup_builtin(ip_str)
    if builtin is not None:
        with _cache_lock:
            _cache[ip_str] = builtin
        try:
            callback(*builtin)
        except Exception:
            pass
        return

    # Онлайн: не плодим дублирующие запросы на один и тот же IP.
    with _inflight_lock:
        if ip_str in _inflight:
            return
        _inflight.add(ip_str)

    def _worker():
        try:
            result = _lookup_online_sync(ip_str)
            with _cache_lock:
                _cache[ip_str] = result
            if result is not None:
                try:
                    callback(*result)
                except Exception:
                    pass
        finally:
            with _inflight_lock:
                _inflight.discard(ip_str)

    threading.Thread(target=_worker, name=f"geo-{ip_str}", daemon=True).start()


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
