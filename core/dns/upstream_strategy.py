"""
UmbraNet — Upstream Strategy
=============================

Управляет тем, КАК UmbraNet опрашивает несколько upstream-серверов
(например primary IPv4 + secondary IPv4 + DoH).

Три режима:
  - "sequential" — по очереди (как было исторически)
  - "parallel"   — все одновременно, берём первый успешный ответ
  - "fastest"    — статистически выбираем лидера, но раз в N запросов
                   "прозваниваем" остальных, чтобы не залипнуть на упавшем

Модуль НЕ умеет сам резолвить — он только выбирает порядок/режим и хранит
счётчик побед. Реальный resolve делает dns_server.py.
"""

import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("UmbraNet.Upstream")


# Допустимые значения config["upstream_mode"]
MODE_SEQUENTIAL = "sequential"
MODE_PARALLEL = "parallel"
MODE_FASTEST = "fastest"

ALL_MODES = (MODE_SEQUENTIAL, MODE_PARALLEL, MODE_FASTEST)
DEFAULT_MODE = MODE_PARALLEL


def normalize_mode(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in ALL_MODES else DEFAULT_MODE


# ── Статистика побед для режима "fastest" ───────────────────────────────────
class UpstreamStats:
    """Запоминает, какой upstream сколько раз отвечал первым / падал.

    Используется только режимом "fastest". Простой EMA (экспоненциальное
    среднее) среднего времени ответа — этого достаточно, чтобы лидер
    автоматически менялся, если у него начались проблемы.
    """

    # Каждые N запросов в "fastest" режиме принудительно идём в parallel,
    # чтобы перепроверить рейтинг (вдруг бывший лидер починился, или
    # текущий лидер сломался). 10 — нормальный баланс.
    PROBE_EVERY_N = 10

    def __init__(self):
        self._lock = threading.Lock()
        # key (label) → {"avg_ms": float, "wins": int, "fails": int, "last_ok": float}
        self._stats = defaultdict(lambda: {
            "avg_ms": 0.0,
            "wins": 0,
            "fails": 0,
            "last_ok": 0.0,
        })
        self._request_counter = 0

    def record_win(self, label: str, latency_ms: float):
        with self._lock:
            s = self._stats[label]
            # EMA с коэффициентом 0.3 — новые измерения весят больше,
            # но один внезапный спайк не выкинет старого лидера.
            if s["avg_ms"] == 0.0:
                s["avg_ms"] = latency_ms
            else:
                s["avg_ms"] = 0.7 * s["avg_ms"] + 0.3 * latency_ms
            s["wins"] += 1
            s["last_ok"] = time.monotonic()

    def record_fail(self, label: str):
        with self._lock:
            self._stats[label]["fails"] += 1

    def get_leader(self, candidate_labels: List[str]) -> Optional[str]:
        """Возвращает label из кандидатов с наименьшим avg_ms.

        Если по кандидату ещё нет данных — он считается «неизвестным»
        и в лидеры не попадает (его сначала пощупает probe).
        """
        with self._lock:
            best_label = None
            best_avg = float("inf")
            for label in candidate_labels:
                s = self._stats.get(label)
                if s is None or s["wins"] == 0:
                    continue
                if s["avg_ms"] < best_avg:
                    best_avg = s["avg_ms"]
                    best_label = label
            return best_label

    def should_probe(self) -> bool:
        """Истина каждые PROBE_EVERY_N вызовов — пора перепроверить рейтинг."""
        with self._lock:
            self._request_counter += 1
            return (self._request_counter % self.PROBE_EVERY_N) == 0

    def snapshot(self):
        """Копия всей статистики (для дебага / возможного UI в будущем)."""
        with self._lock:
            return {k: dict(v) for k, v in self._stats.items()}


# Глобальный singleton — один на приложение
_stats_instance: Optional[UpstreamStats] = None
_stats_lock = threading.Lock()


def get_upstream_stats() -> UpstreamStats:
    global _stats_instance
    with _stats_lock:
        if _stats_instance is None:
            _stats_instance = UpstreamStats()
    return _stats_instance


# ── Гонка запросов ──────────────────────────────────────────────────────────
def race_upstreams(
    tasks: List[Tuple[str, Callable[[], object]]],
    timeout: float = 5.0,
) -> Tuple[Optional[object], Optional[str], float]:
    """Запускает все tasks параллельно, возвращает первый успешный результат.

    tasks: список (label, callable) — callable должен вернуть ответ или None
           (или бросить исключение — оно интерпретируется как fail).

    Возвращает кортеж: (response, winning_label, elapsed_ms).
    Если никто не ответил — (None, None, elapsed_ms).

    Важно: после получения первого успешного ответа остальные запросы
    НЕ отменяются (concurrent.futures это не поддерживает для обычных
    функций), но мы и не ждём их — они просто доработают в фоне.
    """
    if not tasks:
        return None, None, 0.0

    started = time.monotonic()
    # max_workers = число задач, чтобы реально запустить всех параллельно.
    # daemon=True, чтобы поток не мешал выходу программы.
    executor = ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="upstream")
    future_to_label = {}
    try:
        for label, fn in tasks:
            fut = executor.submit(fn)
            future_to_label[fut] = label

        deadline_left = timeout
        remaining = set(future_to_label.keys())
        winner_response = None
        winner_label = None

        while remaining and deadline_left > 0:
            chunk_start = time.monotonic()
            done, _pending = wait(
                remaining, timeout=deadline_left, return_when=FIRST_COMPLETED,
            )
            deadline_left -= max(0.0, time.monotonic() - chunk_start)

            if not done:
                # Дедлайн истёк, никто не успел.
                break

            for fut in done:
                remaining.discard(fut)
                label = future_to_label[fut]
                try:
                    resp = fut.result()
                except Exception as exc:
                    log.debug("upstream %s failed: %s", label, exc)
                    resp = None
                if resp is not None:
                    winner_response = resp
                    winner_label = label
                    break

            if winner_response is not None:
                break

        elapsed_ms = (time.monotonic() - started) * 1000
        return winner_response, winner_label, elapsed_ms
    finally:
        # НЕ блокируем shutdown — futures доработают сами в daemon-потоках.
        executor.shutdown(wait=False, cancel_futures=False)


def reorder_for_fastest(
    candidates: List[Tuple[str, Callable[[], object]]],
    stats: UpstreamStats,
) -> List[Tuple[str, Callable[[], object]]]:
    """Переставляет candidates так, чтобы лидер шёл первым.

    Используется в режиме "fastest" с последовательным запросом:
    сначала пробуем известного лидера, и только если он молчит —
    остальных.
    """
    if not candidates:
        return candidates
    labels = [label for label, _ in candidates]
    leader = stats.get_leader(labels)
    if leader is None:
        return candidates
    leader_task = next(((l, fn) for l, fn in candidates if l == leader), None)
    if leader_task is None:
        return candidates
    others = [(l, fn) for l, fn in candidates if l != leader]
    return [leader_task] + others
