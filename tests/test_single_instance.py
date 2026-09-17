"""
Тесты защиты от второго запуска — `core/single_instance.py` (P2-2).
=================================================================

Зачем модуль. Программа должна запускаться в одном экземпляре: второй клик по
start.bat не создаёт вторую копию, а просит уже открытое окно показаться.
На Windows это именованный mutex, на остальных системах — блокировка файла
(fcntl.flock).

Что было сломано (P2-2). При выходе файл-замок удалялся (`os.remove`). Между
удалением и повторным созданием есть окно: вторая копия успевала открыть путь
уже после unlink, получала НОВЫЙ файл (другой inode) и спокойно брала на нём
блокировку — обе копии считали себя единственными. Последствие на практике:
две программы пишут в один и тот же `umbranet_ui.json` и борются за порт 53.

Что проверяем: второй экземпляр получает отказ; после освобождения новый
экземпляр берёт блокировку снова; файл-замок НЕ исчезает (иначе возвращается
та же гонка); в файле лежит PID владельца.

Запуск: python -m pytest tests/test_single_instance.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import single_instance
from single_instance import SingleInstance

IS_WINDOWS = single_instance.IS_WINDOWS


@pytest.fixture
def guard_factory():
    """Создаёт замки с уникальным именем и подчищает их после теста."""
    made = []

    def make():
        name = f"UmbraNet_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        guard = SingleInstance(name)
        made.append(guard)
        return guard

    yield make

    for guard in made:
        path = guard.lock_path
        guard.release()
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def test_second_copy_is_blocked(guard_factory):
    """Две «копии программы»: вторая получает отказ, первой — можно."""
    first = guard_factory()
    second = guard_factory()          # другое имя — свой замок, тоже свободен
    assert not first.already_running(), "первая копия не смогла занять замок"
    assert not second.already_running(), "вторая копия со своим именем не смогла занять замок"

    # Теперь честный сценарий: то же имя, что у первой копии.
    name = os.path.basename(first.lock_path)
    same = SingleInstance(name)
    try:
        assert same.already_running(), (
            "вторая копия с тем же именем заняла замок — защита от двойного запуска не работает"
        )
    finally:
        same.release()


def test_lock_can_be_taken_after_release(guard_factory):
    """После выхода замок освобождается: следующий запуск программы проходит."""
    first = guard_factory()
    name = os.path.basename(first.lock_path)
    assert not first.already_running()

    first.release()

    second = SingleInstance(name)
    try:
        assert not second.already_running(), (
            "после освобождения замка новая копия не смогла запуститься"
        )
    finally:
        second.release()


@pytest.mark.skipif(IS_WINDOWS, reason="на Windows используется mutex, файла-замка нет")
def test_lock_file_stays_after_release(guard_factory):
    """Файл-замок не удаляется при выходе: удаление возвращало гонку (P2-2)."""
    guard = guard_factory()
    path = guard.lock_path
    assert os.path.exists(path), "файл-замок не создан"

    guard.release()

    assert os.path.exists(path), (
        "файл-замок удалён при выходе — это та самая гонка: вторая копия откроет "
        "новый файл (другой inode) и тоже посчитает себя единственной"
    )


@pytest.mark.skipif(IS_WINDOWS, reason="на Windows используется mutex, файла-замка нет")
def test_lock_file_contains_owner_pid(guard_factory):
    """В замке записан PID владельца — по нему видно, кто держит программу."""
    guard = guard_factory()
    with open(guard.lock_path, encoding="utf-8") as f:
        content = f.read().strip()

    assert content == str(os.getpid()), f"в замке {content!r}, а процесс наш: {os.getpid()}"


def test_release_is_repeatable(guard_factory):
    """Повторный release() не падает: выход из программы бывает и аварийным."""
    guard = guard_factory()
    guard.release()
    guard.release()                      # второй раз должно быть тихо
    assert not guard._mutex and guard._fh is None
