"""
UmbraNet — Fallback State
=========================

Что отвечает на DNS-запросы прямо сейчас и был ли это запасной путь.

Зачем. В резолвере три уровня «запасных» путей, и все три до сих пор были видны
только в текстовом логе, куда пользователь не смотрит:

  1. **запасной транспорт** — выбран, например, DoH, но он не ответил, и запрос
     ушёл через UDP. Это важно: UDP провайдер видит и может подменить;
  2. **запасной провайдер** — xbox-dns не ответил ни одним транспортом, отвечает
     comss.one или профиль пользователя;
  3. **системный DNS** — запрос ушёл в обычный DNS (для доменов вне списка
     маршрутизации это штатный путь, а не сбой; поэтому в статистике он
     считается отдельно и не считается «поломкой»).

Этот модуль — счётчик и «последний ответ»: дёшево (только целые числа и метка
времени под замком), потокобезопасно, живёт в памяти процесса. UI читает
`snapshot()` и рисует карточку «Сейчас отвечает» в «Сети и диагностике».

Сами запросы сюда не попадают: модуль заполняется из resolve-пути
(core/dns/dns_server.py), чтобы не удваивать хранилище журнала запросов.
"""

import threading
import time

# Транспорты, по которым запрос уходит шифрованным (провайдер не видит имя).
ENCRYPTED_TRANSPORTS = ("doh", "dot", "doq", "dnscrypt")

# Провайдер-источник «системный DNS» — не путать с запасным провайдером.
PROVIDER_SYSTEM = "system"
PROVIDER_UNKNOWN = ""


class FallbackState:
    """Текущее состояние ответов: кто отвечает и сколько было запасных путей."""

    def __init__(self):
        self._lock = threading.Lock()
        # «Последний ответ провайдера» и «последний ответ системного DNS» храним
        # РАЗДЕЛЬНО. Причина: системный DNS отвечает на каждый запрос домена вне
        # списка маршрутизации — то есть постоянно, вперемешку с маршрутизируемыми.
        # Если писать их в одно поле, заголовок индикатора мигал бы «системный DNS»
        # почти всегда и перестал бы что-либо значить.
        self._last = {
            "provider_id": PROVIDER_UNKNOWN,
            "provider_name": "",
            "transport": "",
            "preferred_transport": "",
            "encrypted": False,
            "fallback_transport": False,
            "fallback_provider": False,
            "last_answer_ts": 0.0,
            "last_answer_domain": "",
        }
        self._last_system = {"domain": "", "ts": 0.0}
        self._counters = {
            "answers": 0,             # ответов от провайдеров (xbox-dns и запасные)
            "transport_fallback": 0,  # из них через запасной транспорт
            "provider_fallback": 0,   # из них от запасного провайдера
            "encrypted": 0,           # из них шифрованным транспортом
            "plain": 0,               # из них открытым (udp)
            "system_answers": 0,      # ответов через системный DNS
            "system_checked": False,  # был ли вообще системный путь
        }
        self._started_ts = time.time()
        self._last_switch_ts = 0.0    # когда последний раз сменился «кто отвечает»

    # ── запись ──────────────────────────────────────────────────────────────

    def record_provider_answer(self, provider_id: str, provider_name: str,
                              transport: str, preferred_transport: str,
                              provider_index: int = 0, domain: str = "",
                              is_primary: bool = True) -> None:
        """Ответил провайдер (xbox-dns или запасной) — фиксируем транспорт и кто это был."""
        transport = (transport or "").strip().lower()
        preferred = (preferred_transport or "").strip().lower()
        fallback_transport = bool(preferred) and transport != preferred
        fallback_provider = not is_primary or provider_index > 0
        encrypted = transport in ENCRYPTED_TRANSPORTS
        now = time.time()
        with self._lock:
            previous = (self._last["provider_id"], self._last["transport"])
            self._last.update({
                "provider_id": provider_id or PROVIDER_UNKNOWN,
                "provider_name": provider_name or provider_id or "",
                "transport": transport,
                "preferred_transport": preferred,
                "encrypted": encrypted,
                "fallback_transport": fallback_transport,
                "fallback_provider": fallback_provider,
                "last_answer_ts": now,
                "last_answer_domain": domain or "",
            })
            c = self._counters
            c["answers"] += 1
            c["encrypted" if encrypted else "plain"] += 1
            if fallback_transport:
                c["transport_fallback"] += 1
            if fallback_provider:
                c["provider_fallback"] += 1
            if previous != (self._last["provider_id"], transport):
                self._last_switch_ts = now

    def record_system_answer(self, domain: str = "", transport: str = "system") -> None:
        """Ответ пришёл через системный DNS (не через профили UmbraNet).

        Отдельный счётчик и отдельная метка «кто отвечает»: смешивать системный
        DNS с запасным провайдером нельзя — для доменов вне списка маршрутизации
        это штатный путь, а не признак поломки.
        """
        now = time.time()
        with self._lock:
            # Заголовок индикатора НЕ трогаем: он про профили UmbraNet.
            self._last_system.update({"domain": domain or "", "ts": now})
            self._counters["system_answers"] += 1
            self._counters["system_checked"] = True

    def reset(self) -> None:
        with self._lock:
            self._last.update({
                "provider_id": PROVIDER_UNKNOWN,
                "provider_name": "",
                "transport": "",
                "preferred_transport": "",
                "encrypted": False,
                "fallback_transport": False,
                "fallback_provider": False,
                "last_answer_ts": 0.0,
                "last_answer_domain": "",
            })
            self._last_system.update({"domain": "", "ts": 0.0})
            for key in self._counters:
                self._counters[key] = 0
            self._started_ts = time.time()
            self._last_switch_ts = 0.0

    # ── чтение ──────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Готовые для UI данные: последний ответ, счётчики и понятный вердикт."""
        with self._lock:
            last = dict(self._last)
            counters = dict(self._counters)
            started = self._started_ts
            switch_ts = self._last_switch_ts

        answers = counters["answers"]
        system_answers = counters["system_answers"]
        total = answers + system_answers
        last["system_share"] = (system_answers / total) if total else 0.0
        last["system_answers"] = system_answers
        last["last_system_domain"] = self._last_system["domain"]
        last["last_system_ts"] = self._last_system["ts"]
        last["counters"] = counters
        last["started_ts"] = started
        last["seconds_since_switch"] = (time.time() - switch_ts) if switch_ts else -1.0

        # Вердикт для заголовка индикатора — один из четырёх, чтобы интерфейс не
        # додумывал логику сам:
        #   idle            — ещё ничего не отвечало;
        #   system_only     — отвечал только системный DNS (профили UmbraNet молчат);
        #   fallback_provider — ответил запасной провайдер;
        #   fallback        — ответил запасной транспорт;
        #   primary         — штатный путь.
        if answers == 0 and system_answers == 0:
            last["state"] = "idle"
        elif answers == 0:
            last["state"] = "system_only"
        elif last["fallback_provider"]:
            last["state"] = "fallback_provider"
        elif last["fallback_transport"]:
            last["state"] = "fallback"
        else:
            last["state"] = "primary"
        return last


_instance = None
_instance_init_lock = threading.Lock()


def get_fallback_state() -> FallbackState:
    """Единственный трекер на процесс (DNS-запросы идут из нескольких потоков)."""
    global _instance
    if _instance is None:
        with _instance_init_lock:
            if _instance is None:
                _instance = FallbackState()
    return _instance


def reset_fallback_state() -> None:
    """Сброс для тестов и для «Стоп» (статистика относится к текущему запуску)."""
    get_fallback_state().reset()
