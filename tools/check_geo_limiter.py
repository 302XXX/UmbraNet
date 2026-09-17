"""
Ручная проверка лимитера геолокации карты (H5).
==============================================

Зачем. Бесплатный ip-api.com разрешает около 45 запросов в минуту с одного
адреса. Если лимитер сломается (или кто-то поднимет его настройки), сервис
ответит 429 — и карта останется без геолокации. Этот скрипт показывает, как
реально ведёт себя модуль `Map/core/geo.py`: с какой частотой уходят запросы,
попадает ли в минуту больше предела, работает ли пауза после бана, что лежит
в дисковом кэше.

Два режима:

  python tools/check_geo_limiter.py
      Сухой прогон: 60 обращений к геолокации на виртуальных часах, сеть
      подменена заглушкой. Мгновенно, интернет не нужен и лимит не тратится.
      Показывает шаг запросов и то, что 41-й запрос в минуту ждёт окна.

  python tools/check_geo_limiter.py --live 12
      Настоящие запросы к ip-api.com для 12 адресов (список ниже). Идёт по
      живому лимитеру, поэтому занимает около минуты на десяток адресов.
      ВНИМАНИЕ: это настоящие запросы и настоящий расход квоты сервиса.
"""

from __future__ import annotations

import argparse
import itertools
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from Map.core import geo

# Адреса для проверки: диапазон /16, которого нет в офлайн-таблице карты.
# 1.1.1.1 и 8.8.8.8 специально НЕ берём: они отвечают из офлайн-таблицы и в сеть
# не пойдут — а нам нужно показать именно сетевой путь и лимитер.
PROBE_RANGE = "45.33"


def probe_ips(count: int) -> list[str]:
    """`count` разных публичных адресов подряд."""
    return [f"{PROBE_RANGE}.{i // 250 + 32}.{i % 250 + 1}" for i in range(count)]


class _StubResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _stub_transport(url: str, params: dict, timeout: float):
    """Заглушка сети для сухого прогона: отвечает мгновенно, без интернета."""
    return _StubResponse({"status": "success", "country": "Нидерланды",
                          "city": "Амстердам", "lat": 52.37, "lon": 4.90})


class _Clock:
    """Виртуальные часы: сухой прогон не ждёт реальных пауз лимитера."""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.t += max(0.0, float(seconds))


def _dry_run(count: int) -> None:
    clock = _Clock()
    sent_at: list[float] = []

    def timing_transport(url: str, params: dict, timeout: float):
        """"Сеть": отмечаем момент запроса на виртуальных часах и отвечаем сразу."""
        sent_at.append(clock.now())
        return _stub_transport(url, params, timeout)

    geo._transport = timing_transport                 # подмена ради проверки
    geo._now_mono = clock.now
    geo._now_wall = clock.now
    geo._sleep = clock.sleep
    geo.set_cache_path(os.path.join(os.environ.get("TEMP", "/tmp"), "umbranet_geo_check.json"))
    geo.reset_state(clear_disk=True)

    print("Сухой прогон: сеть подменена заглушкой, часы виртуальные.\n")
    ips = probe_ips(count)
    started = clock.now()
    for ip in ips:
        geo.geolocate_sync(ip)

    per_minute: dict[int, int] = {}
    for t in sent_at:
        minute = int((t - started) // 60)
        per_minute[minute] = per_minute.get(minute, 0) + 1

    gaps = [b - a for a, b in itertools.pairwise(sent_at)]
    print(f"Адресов прогнали:      {len(ips)}")
    print(f"Из них ушло в сеть:    {len(sent_at)} (остальное — офлайн-таблица и кэш)")
    print(f"Виртуального времени:  {clock.now() - started:.1f} с "
          f"(из них паузы лимитера {clock.slept:.1f} с)")
    if gaps:
        print(f"Шаг между запросами:   минимум {min(gaps):.2f} с, "
              f"максимум {max(gaps):.2f} с (настройка шага — {geo._MIN_INTERVAL_S} с)")
    print(f"Запросов по минутам:   {dict(sorted(per_minute.items()))} "
          f"(предел — {geo._RATE_LIMIT_PER_MIN} в минуту)")
    print(f"Сводка модуля:         {geo.stats()}")


def _live_run(count: int) -> None:
    print(f"Живой прогон: {count} настоящих запросов к ip-api.com.")
    print("Учитывайте расход квоты сервиса (~45 запросов в минуту).\n")
    geo.reset_state(clear_disk=True)
    started = time.monotonic()
    hits, misses = 0, 0
    for ip in probe_ips(count):
        before = time.monotonic()
        result = geo.geolocate_sync(ip)
        waited = time.monotonic() - before
        if result is None:
            misses += 1
            print(f"  {ip:<16} — нет данных (ждали {waited:5.1f} с)")
        else:
            hits += 1
            print(f"  {ip:<16} — {result[2]} ({result[0]:.2f}, {result[1]:.2f}), "
                  f"ждали {waited:5.1f} с")
    print(f"\nИтого: точек найдено {hits}, без данных {misses}, "
          f"времени {time.monotonic() - started:.1f} с")
    print("Сводка модуля:", geo.stats())
    print("Файл кэша:", geo.cache_path())


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка лимитера геолокации карты (H5)")
    parser.add_argument("--live", type=int, default=0, metavar="N",
                        help="сделать N настоящих запросов к ip-api.com")
    parser.add_argument("--count", type=int, default=60,
                        help="сколько адресов прогнать в сухом режиме (по умолчанию 60)")
    args = parser.parse_args()

    if args.live:
        _live_run(args.live)
    else:
        _dry_run(args.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
