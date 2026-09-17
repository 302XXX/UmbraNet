"""
Тесты диспетчера задач (список доменов и процессов на вкладке «Маршрутизация»).

Заголовок блока раньше назывался «Все активные домены и процессы». Пользователь
переименовал его в «Диспетчер задач» (значок блокнота остался на прежнем месте).
Смысл этих тестов — не дать названию разъехаться снова и не оставить в программе
текстов-подсказок, которые ссылаются на блок, которого больше нет: пользователь
должен читать в подсказке ровно то, что видит на экране.

Запуск: python -m pytest tests/test_task_manager_title.py
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if p not in sys.path:
        sys.path.insert(0, p)

TITLE = "Диспетчер задач"
OLD_TITLE = "Все активные домены и процессы"

# Файлы, где блок упоминается: заголовок, подсказки-диалоги, текст ошибки окна.
SOURCE_FILES = (
    "umbranet/views/routing.py",
    "umbranet/app.py",
    "umbranet/engine_adapter.py",
    "umbranet/widgets/manual_canvas.py",
)


def read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_block_title_is_task_manager():
    """Заголовок блока на вкладке — «Диспетчер задач», и значок на месте."""
    source = read("umbranet/views/routing.py")
    assert f'CategorySection("{TITLE}"' in source, (
        f"заголовок блока должен быть «{TITLE}»"
    )
    # Значок блокнота передаётся вторым аргументом и должен остаться на месте.
    match = re.search(r'CategorySection\(\s*"([^"]+)"\s*,\s*"([^"]*)"', source)
    assert match, "не найдена строка создания секции"
    assert match.group(1) == TITLE, f"заголовок секции: {match.group(1)!r}"
    assert match.group(2) == "📋", f"значок секции должен остаться блокнотом, а не {match.group(2)!r}"


def test_old_title_is_gone_from_sources():
    """Старого названия не осталось ни в заголовке, ни в подсказках.

    Проверяем не полное прежнее название, а его ядро: в подсказках оно встречалось
    и в укороченном виде («Все активные домены» — без «и процессы»), и такое
    упоминание точно так же отправляло бы человека в несуществующий блок.
    """
    for rel in SOURCE_FILES:
        source = read(rel)
        assert OLD_TITLE not in source, (
            f"{rel}: осталось старое название «{OLD_TITLE}»"
        )
        lowered = source.lower()
        assert "активные домены" not in lowered, (
            f"{rel}: осталось упоминание прежнего названия («активные домены») — "
            f"пользователь искал бы блок, которого нет"
        )


def test_hints_point_to_the_visible_block_name():
    """КАЖДАЯ подсказка, отправляющая в этот блок, зовёт его новым именем.

    Важно проверять каждую по отдельности: если поправить только одну из двух,
    вторая продолжит отправлять человека к блоку со старым названием.
    """
    phrases = (
        ("umbranet/app.py", "в блоке «Диспетчер задач»",
         "подсказка диалога про цели DPI"),
        ("umbranet/app.py", "или добавьте домен вручную в «Диспетчер задач»",
         "подсказка диалога про запуск без сервисов"),
        ("umbranet/engine_adapter.py", "«Маршрутизация» → «Диспетчер задач»",
         "текст ошибки при пустых целях DPI"),
    )
    for rel, phrase, what in phrases:
        assert phrase in read(rel), f"{rel}: {what} должна звать блок «{TITLE}»"


def test_block_header_renders_new_title():
    """Проверяем именно то, что человек видит на экране — текст в шапке блока."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.views.routing import CategoryHeader

    header = CategoryHeader(TITLE, "📋", "#6c8cff", "#8b5cf6")
    labels = [lbl.text() for lbl in header.findChildren(QtWidgets.QLabel)]
    # Заголовок рисуется капсом (style: title.upper()) — проверяем без учёта регистра.
    assert any(lbl.upper() == TITLE.upper() for lbl in labels), (
        f"в шапке блока нет надписи «{TITLE}»: {labels}"
    )
    assert "📋" in labels, f"значок блокнота потерялся: {labels}"
    header.deleteLater()
