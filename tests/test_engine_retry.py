"""
Тесты запуска движка после остановки — повторы вместо одной паузы (P2-3).
=======================================================================

Что было не так. При перезапуске (`restart`) код останавливал движок и спал
фиксированные 0.3 с: «дать ОС освободить порт 53». Фиксированная пауза — это
лотерея: на медленной машине, при занятом «отставшим» процессе порте или под
антивирусом её не хватало, и человек видел «DPI не запустился» — хотя через миг
всё бы встало. Обратная сторона тоже плоха: на быстрой машине мы всё равно
ждали 0.3 с на каждой остановке-запуске.

Что стало: `_start_engine_with_retry()` — несколько попыток с нарастающими
паузами (0.3 → 0.7 → 1.5 с). Если порт освободился чуть позже, запуск всё
равно удаётся; если причина настоящая — получим честный отказ, и это видно в
логе. Время вынесено в параметры, поэтому тесты идут мгновенно.

Запуск: python -m pytest tests/test_engine_retry.py
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from umbranet import app as app_mod

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Engine:
    """Движок-заглушка: помнит вызовы и отдаёт заранее заданные ответы start()."""

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.starts = 0
        self.stops = 0

    def start(self) -> bool:
        self.starts += 1
        return self.answers.pop(0) if self.answers else False

    def stop(self) -> None:
        self.stops += 1


def test_start_ok_on_first_try():
    """Обычный случай: запустилось сразу — лишних попыток и пауз нет."""
    engine = _Engine([True])
    sleeps: list[float] = []

    assert app_mod._start_engine_with_retry(engine, sleeper=sleeps.append) is True
    assert engine.starts == 1, f"движок запускали {engine.starts} раз вместо одного"
    assert sleeps == [0.3], f"пауза перед первой попыткой не та: {sleeps}"


def test_retries_until_port_is_free():
    """Порт освободился не сразу: вторая попытка спасает запуск."""
    engine = _Engine([False, True])
    sleeps: list[float] = []

    assert app_mod._start_engine_with_retry(engine, sleeper=sleeps.append) is True
    assert engine.starts == 2
    assert sleeps == [0.3, 0.7], f"паузы между попытками не нарастают: {sleeps}"


def test_waits_longer_each_time_and_then_gives_up():
    """Настоящая причина (не порт): попытки кончаются, отказ честный."""
    engine = _Engine([False, False, False])
    sleeps: list[float] = []

    assert app_mod._start_engine_with_retry(engine, sleeper=sleeps.append) is False
    assert engine.starts == 3, f"попыток {engine.starts}, а ждали три"
    assert sleeps == [0.3, 0.7, 1.5], f"паузы {sleeps} вместо [0.3, 0.7, 1.5]"
    assert sleeps == sorted(sleeps), "паузы должны расти, а не уменьшаться"


def test_retry_plan_is_configurable():
    """План повторов задаётся снаружи: тесты и будущие правки не привязаны к числам."""
    engine = _Engine([False, True])
    sleeps: list[float] = []

    assert app_mod._start_engine_with_retry(engine, waits=(0.1, 0.2), sleeper=sleeps.append)
    assert sleeps == [0.1, 0.2]


def test_restart_worker_uses_retry(monkeypatch):
    """Воркер перезапуска действительно ходит через повторы, а не через старую паузу."""
    engine = _Engine([True])
    called: list = []

    def fake_retry(eng, **kwargs):
        called.append(eng)
        return True

    monkeypatch.setattr(app_mod, "_start_engine_with_retry", fake_retry)

    results: list = []
    worker = app_mod._EngineWorker(engine, "restart")
    worker.finished.connect(lambda action, ok: results.append((action, ok)))
    worker.run()

    assert engine.stops == 1, "перед запуском движок не остановили"
    assert called == [engine], "перезапуск прошёл мимо повторов (осталась фиксированная пауза)"
    assert results == [("restart", True)], f"воркер отчитался так: {results}"


def test_failed_restart_is_reported(monkeypatch):
    """Неудачный перезапуск виден вызывающему: ok=False, а не тихое «всё хорошо»."""
    engine = _Engine([False])
    monkeypatch.setattr(app_mod, "_start_engine_with_retry", lambda eng, **kw: False)

    results: list = []
    worker = app_mod._EngineWorker(engine, "restart")
    worker.finished.connect(lambda action, ok: results.append((action, ok)))
    worker.run()

    assert results == [("restart", False)]
