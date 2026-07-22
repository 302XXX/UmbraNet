"""
UmbraNet — фоновое обновление списка bogus-IP провайдеров РФ.

Проблема, которую решаем:
  Список bogus-IP в bogus_ips.py статичен и устаревает — провайдеры
  периодически меняют адреса своих страниц «Доступ ограничен».
  Через 3-6 месяцев часть заглушек перестаёт детектироваться.

Решение:
  Фоновый поток BogusUpdater раз в 24 часа скачивает актуальный JSON
  с GitHub (из самого репозитория UmbraNet). Скачанный список:
    1. Валидируется (битые строки дропаются, не ломая работу)
    2. Мержится со встроенным builtin-списком (builtin — всегда база)
    3. Сохраняется на диск рядом с config.json (кэш для офлайн-запуска)
    4. Применяется горячо — без перезапуска DNS-сервера

При офлайн-запуске / недоступности URL:
  - Используется последний сохранённый диск-кэш
  - Если кэша нет — только builtin (поведение как раньше)
  - Ошибки сети логируются на DEBUG-уровне, не падают на пользователя

Архитектурные решения:
  - Один фоновый daemon-поток — не блокирует запуск и работу DNS
  - Обновление атомарно: новый индекс применяется целиком или не применяется
  - Callback on_update(ips, subnets) вызывается в потоке обновления;
    UmbraNetResolver сбрасывает _bogus_cache — следующий запрос подхватит новое
  - Нет внешних зависимостей кроме urllib (stdlib)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from typing import Callable, Optional

log = logging.getLogger("UmbraNet.BogusUpdater")

# ── Настройки ─────────────────────────────────────────────────────────────────

# URL удалённого списка: raw-файл прямо из репозитория UmbraNet на GitHub.
# Меняем только этот URL, если репо переедет.
REMOTE_URL = (
    "https://raw.githubusercontent.com/302XXX/UmbraNet/main/bogus_ips_remote.json"
)

# Локальный бандл: файл bogus_ips_remote.json поставляется вместе с программой
# и лежит в корне репо (два уровня выше core/). Это первичный источник —
# работает без сети, всегда актуален на момент установки.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_BUNDLED_FILE = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "bogus_ips_remote.json")
)

# Имя диск-кэша рядом с config.json
CACHE_FILENAME = "bogus_ips_cache.json"

# Интервал фонового обновления (секунды). 86400 = 24 часа.
UPDATE_INTERVAL = 86_400

# Таймаут HTTP-запроса
HTTP_TIMEOUT = 10.0

# Минимальный интервал между попытками при ошибке сети (секунды)
RETRY_INTERVAL = 3_600  # 1 час


# ── Валидация входных данных ──────────────────────────────────────────────────

def _validate_ip(raw: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Парсит строку как IP-адрес. Возвращает объект или None при ошибке."""
    try:
        return ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return None


def _validate_subnet(raw: str) -> Optional[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Парсит строку как CIDR-подсеть. Возвращает объект или None при ошибке."""
    try:
        return ipaddress.ip_network(str(raw).strip(), strict=False)
    except ValueError:
        return None


def _parse_remote_json(data: bytes) -> tuple[list[str], list[str]]:
    """
    Разбирает JSON из удалённого источника.
    Возвращает (ips: list[str], subnets: list[str]) — только валидные значения.
    Невалидные строки молча дропаются — не ломаем работу из-за одного битого IP.
    """
    try:
        obj = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Невалидный JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise ValueError("Ожидается JSON-объект верхнего уровня")

    raw_ips = obj.get("bogus_ips", [])
    raw_subnets = obj.get("bogus_subnets", [])

    if not isinstance(raw_ips, list) or not isinstance(raw_subnets, list):
        raise ValueError("bogus_ips и bogus_subnets должны быть массивами")

    valid_ips: list[str] = []
    for raw in raw_ips:
        ip = _validate_ip(str(raw))
        if ip is not None:
            valid_ips.append(str(ip))
        else:
            log.debug("Дропнут невалидный IP из remote: %r", raw)

    valid_subnets: list[str] = []
    for raw in raw_subnets:
        net = _validate_subnet(str(raw))
        if net is not None:
            valid_subnets.append(str(net))
        else:
            log.debug("Дропнута невалидная подсеть из remote: %r", raw)

    return valid_ips, valid_subnets


# ── Диск-кэш ─────────────────────────────────────────────────────────────────

def _cache_path(config_dir: str) -> str:
    return os.path.join(config_dir, CACHE_FILENAME)


def _save_cache(config_dir: str, ips: list[str], subnets: list[str]) -> None:
    """Сохраняет актуальный список на диск для офлайн-запуска."""
    path = _cache_path(config_dir)
    try:
        payload = {
            "_updated_at": time.time(),
            "bogus_ips": ips,
            "bogus_subnets": subnets,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # атомарная замена файла
        log.debug("Диск-кэш bogus-IP сохранён: %d IP, %d подсетей", len(ips), len(subnets))
    except Exception as exc:
        log.warning("Не удалось сохранить кэш bogus-IP: %s", exc)


def load_cached(config_dir: str) -> tuple[list[str], list[str]]:
    """
    Загружает диск-кэш bogus-IP.
    Возвращает (ips, subnets) или ([], []) если кэша нет или он битый.
    """
    path = _cache_path(config_dir)
    if not os.path.exists(path):
        return [], []
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        ips = [str(x) for x in obj.get("bogus_ips", []) if _validate_ip(str(x))]
        subnets = [str(x) for x in obj.get("bogus_subnets", []) if _validate_subnet(str(x))]
        log.debug(
            "Диск-кэш bogus-IP загружен: %d IP, %d подсетей (возраст: %.0f ч)",
            len(ips), len(subnets),
            (time.time() - float(obj.get("_updated_at", 0))) / 3600,
        )
        return ips, subnets
    except Exception as exc:
        log.warning("Не удалось прочитать кэш bogus-IP (%s): %s", path, exc)
        return [], []


# ── Локальный бандл ──────────────────────────────────────────────────────────

def _read_local_bundled(path: str = LOCAL_BUNDLED_FILE) -> bytes:
    """
    Читает локальный файл bogus_ips_remote.json, поставляемый вместе с программой.
    Бросает FileNotFoundError если файл не найден, OSError при ошибке чтения.
    """
    with open(path, "rb") as f:
        return f.read()


def _load_from_local(path: str = LOCAL_BUNDLED_FILE) -> tuple[list[str], list[str]] | None:
    """
    Загружает список из локального бандла.
    path — путь к файлу (по умолчанию LOCAL_BUNDLED_FILE из корня программы).
    Возвращает (ips, subnets) при успехе или None при любой ошибке.
    """
    try:
        data = _read_local_bundled(path)
        ips, subnets = _parse_remote_json(data)
        if ips or subnets:
            log.debug(
                "BogusUpdater: загружен локальный бандл (%d IP, %d подсетей)",
                len(ips), len(subnets),
            )
            return ips, subnets
        return None
    except FileNotFoundError:
        log.debug("BogusUpdater: локальный бандл не найден (%s)", path)
        return None
    except Exception as exc:
        log.debug("BogusUpdater: ошибка чтения локального бандла: %s", exc)
        return None


# ── HTTP-загрузчик ────────────────────────────────────────────────────────────

def _fetch_remote(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    """
    Скачивает JSON по URL. Использует только urllib (stdlib).
    Бросает urllib.error.URLError или OSError при ошибке сети/таймауте.
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "UmbraNet/1.0 bogus-ip-updater"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ── Основной класс ────────────────────────────────────────────────────────────

class BogusUpdater:
    """
    Фоновый обновлятель списка bogus-IP.

    Использование:
        updater = BogusUpdater(
            config_dir=os.path.dirname(config_json_path),
            on_update=resolver._invalidate_bogus_cache,
        )
        updater.start()   # запускаем при старте DNS-сервера
        updater.stop()    # останавливаем при остановке

    Параметры:
        config_dir  — директория рядом с config.json (туда пишем кэш)
        on_update   — callback(ips: list[str], subnets: list[str]),
                      вызывается когда получен новый актуальный список.
                      Может быть None — тогда обновление только кэшируется.
        url         — URL удалённого JSON (по умолчанию REMOTE_URL)
        interval    — интервал обновления в секундах (по умолчанию 24 часа)
    """

    def __init__(
        self,
        config_dir: str,
        on_update: Optional[Callable[[list[str], list[str]], None]] = None,
        url: str = REMOTE_URL,
        interval: float = UPDATE_INTERVAL,
    ):
        self.config_dir = config_dir
        self.on_update = on_update
        self.url = url
        self.interval = interval

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_success: float = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Запускает фоновый поток обновления."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="UmbraNet-BogusUpdater",
                daemon=True,
            )
            self._thread.start()
            log.info("BogusUpdater запущен (интервал: %d ч)", self.interval // 3600)

    def stop(self) -> None:
        """Останавливает фоновый поток (ждёт не более 2 сек)."""
        self._stop_event.set()
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        log.debug("BogusUpdater остановлен")

    # ── Внутренняя логика ────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Главный цикл фонового потока."""
        # При первом запуске сразу пробуем обновиться
        self._try_update()

        while not self._stop_event.wait(timeout=60.0):
            now = time.time()
            elapsed = now - self._last_success
            if elapsed >= self.interval:
                self._try_update()

    def _try_update(self) -> None:
        """Одна попытка обновить список.

        Стратегия (offline-first):
          1. Читаем локальный бандл bogus_ips_remote.json из корня программы —
             работает без сети, всегда актуален на момент установки.
          2. Пробуем скачать более свежую версию с GitHub (может не сработать
             при отсутствии сети — это нормально, не ошибка).
          Применяем лучший из двух источников (сетевой приоритетнее если он свежее).
        """
        ips, subnets = [], []
        source = "none"

        # ── Шаг 1: локальный бандл ───────────────────────────────────────────
        local = _load_from_local()
        if local:
            ips, subnets = local
            source = "local"

        # ── Шаг 2: сетевое обновление (опционально) ──────────────────────────
        log.debug("BogusUpdater: попытка сетевого обновления с %s", self.url)
        try:
            data = _fetch_remote(self.url, timeout=HTTP_TIMEOUT)
            net_ips, net_subnets = _parse_remote_json(data)
            if net_ips or net_subnets:
                ips, subnets = net_ips, net_subnets
                source = "network"
                log.debug("BogusUpdater: сетевой список получен, приоритет над локальным")
        except urllib.error.URLError as exc:
            log.debug("BogusUpdater: сеть недоступна (%s)", exc.reason)
        except TimeoutError:
            log.debug("BogusUpdater: таймаут сетевого запроса")
        except ValueError as exc:
            log.warning("BogusUpdater: невалидный сетевой ответ: %s", exc)
        except Exception as exc:
            log.debug("BogusUpdater: сетевая ошибка: %s", exc)

        # ── Применяем результат ───────────────────────────────────────────────
        if not ips and not subnets:
            log.debug("BogusUpdater: нет данных ни из одного источника")
            return

        _save_cache(self.config_dir, ips, subnets)
        self._last_success = time.time()

        if self.on_update is not None:
            try:
                self.on_update(ips, subnets)
            except Exception as exc:
                log.error("BogusUpdater: ошибка в on_update callback: %s", exc)

        log.info(
            "BogusUpdater: список обновлён из [%s] — %d IP, %d подсетей",
            source, len(ips), len(subnets),
        )

    @property
    def last_updated(self) -> Optional[float]:
        """Unix-timestamp последнего успешного обновления, или None."""
        return self._last_success or None

    def force_update(self) -> bool:
        """
        Принудительное обновление прямо сейчас (блокирующий вызов).
        Вызывается из кнопки «Обновить список» в UI.

        Стратегия (offline-first):
          1. Читает локальный бандл — гарантирует успех даже без сети.
          2. Пробует загрузить из сети — если удалось, использует сетевую версию.
        Возвращает True если хотя бы один источник дал данные.
        """
        ips, subnets = [], []
        source = "none"

        # Шаг 1: локальный бандл (всегда пробуем)
        local = _load_from_local()
        if local:
            ips, subnets = local
            source = "local"

        # Шаг 2: сеть (если доступна — заменяет локальный)
        try:
            data = _fetch_remote(self.url, timeout=HTTP_TIMEOUT)
            net_ips, net_subnets = _parse_remote_json(data)
            if net_ips or net_subnets:
                ips, subnets = net_ips, net_subnets
                source = "network"
        except Exception as exc:
            log.debug("BogusUpdater: сеть при force_update недоступна: %s", exc)

        if not ips and not subnets:
            log.warning("BogusUpdater: force_update — нет данных ни из одного источника")
            return False

        _save_cache(self.config_dir, ips, subnets)
        self._last_success = time.time()
        if self.on_update is not None:
            try:
                self.on_update(ips, subnets)
            except Exception as exc:
                log.error("BogusUpdater: ошибка в on_update при force_update: %s", exc)

        log.info(
            "BogusUpdater: force_update выполнен из [%s] — %d IP, %d подсетей",
            source, len(ips), len(subnets),
        )
        return True
