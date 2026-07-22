"""
UmbraNet — Provider Health
===========================

Лёгкий трекер «живости» провайдеров (xbox-dns, comss.one, пользовательские).
Питает две вещи:
  1) понятный статус для пользователя («🟢 работает / 🔴 не работает»);
  2) выбор провайдера при failover (не долбиться в недавно упавший).

Не делает активных проверок сам — состояние обновляется из resolve-пути
(record_success / record_failure) по факту реальных запросов. Дополнительно
есть check_now() для кнопки «Проверить» в UI.

Потокобезопасен. Состояние — в оперативной памяти.
"""

import threading
import time

# Статусы провайдера
STATUS_UNKNOWN = "unknown"   # ещё не пробовали
STATUS_UP = "up"             # последний запрос удался
STATUS_DOWN = "down"         # последние запросы падают


class ProviderHealth:
    def __init__(self, fail_threshold: int = 2):
        # provider_id -> {"status","last_ok","last_fail","fails","name"}
        self._state = {}
        self._lock = threading.Lock()
        # сколько подряд неудач, чтобы считать провайдера "down"
        self.fail_threshold = max(1, int(fail_threshold))

    def _entry(self, provider_id, name=None):
        e = self._state.get(provider_id)
        if e is None:
            e = {
                "status": STATUS_UNKNOWN,
                "last_ok": 0.0,
                "last_fail": 0.0,
                "fails": 0,
                "name": name or provider_id,
            }
            self._state[provider_id] = e
        if name:
            e["name"] = name
        return e

    def record_success(self, provider_id, name=None):
        with self._lock:
            e = self._entry(provider_id, name)
            e["status"] = STATUS_UP
            e["last_ok"] = time.time()
            e["fails"] = 0

    def record_failure(self, provider_id, name=None):
        with self._lock:
            e = self._entry(provider_id, name)
            e["last_fail"] = time.time()
            e["fails"] += 1
            if e["fails"] >= self.fail_threshold:
                e["status"] = STATUS_DOWN

    def status_of(self, provider_id) -> str:
        with self._lock:
            e = self._state.get(provider_id)
            return e["status"] if e else STATUS_UNKNOWN

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def overall_status(self, provider_ids=None) -> str:
        """Единый статус для UI по списку провайдеров.

        up      — хотя бы один провайдер сейчас «up»;
        down    — есть данные и ВСЕ известные провайдеры «down»;
        unknown — ещё ничего не пробовали.
        """
        with self._lock:
            items = self._state
            if provider_ids is not None:
                items = {k: v for k, v in items.items() if k in provider_ids}
            if not items:
                return STATUS_UNKNOWN
            statuses = [v["status"] for v in items.values()]
            if any(s == STATUS_UP for s in statuses):
                return STATUS_UP
            if statuses and all(s == STATUS_DOWN for s in statuses):
                return STATUS_DOWN
            return STATUS_UNKNOWN

    def reset(self):
        with self._lock:
            self._state.clear()


_instance = None


def get_provider_health() -> ProviderHealth:
    global _instance
    if _instance is None:
        _instance = ProviderHealth()
    return _instance
