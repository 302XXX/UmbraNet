"""
Регрессионные тесты UI-гардов UmbraNet.

Покрывают два бага, найденных 15.09.2026:

1. Экранирование QSS. В обычных (НЕ f-) строках писались «}}» вместо «}».
   Python оставлял обе скобки, Qt не мог разобрать stylesheet и молча
   ВЫБРАСЫВАЛ ВСЕ правила после лишней скобки. Симптом: кнопка «Старт»
   не становилась серой (правило :disabled не применялось), хотя
   setEnabled(False) вызывался. Тест ловит любой вернувшийся «}}».

2. Крестики у защищённых процессов. chrome.exe / msedge.exe / firefox.exe
   нельзя удалить из маршрутизации, но ✕ рисовался и клик молча ничего
   не делал. Теперь у них замок, а клик по зоне удаления игнорируется.

Запуск: python -m pytest tests/test_ui_guards.py
"""

from __future__ import annotations

import ast
import io
import os
import pathlib
import sys
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── 1. Экранирование QSS ─────────────────────────────────────────────────────

QSS_MARKERS = ("border", "background", "font-size", "padding", "radius", "color:",
               "QPushButton", "QSlider", "QListWidget", "QProgressBar", "QFrame",
               "QLineEdit", "QComboBox", "QMenu", "QScrollBar", "QToolTip")


def _qss_expression_regions(src: str, tree: ast.AST) -> list[tuple[int, int]]:
    """Диапазоны выражений, которые собирают QSS (по маркерам внутри)."""
    lines = src.split("\n")
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln) + 1

    def abs_of(pos):
        return starts[pos[0] - 1] + pos[1]

    parents = {}
    for node in ast.walk(tree):
        for ch in ast.iter_child_nodes(node):
            parents[ch] = node

    regions = []
    for node in ast.walk(tree):
        has_escape = any(
            isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            and ("{{" in sub.value or "}}" in sub.value)
            for sub in ast.walk(node)
        )
        if not has_escape:
            continue
        cur = node
        while cur in parents and not isinstance(
                parents[cur], (ast.Expr, ast.Return, ast.Assign, ast.AnnAssign)):
            cur = parents[cur]
        if not hasattr(cur, "lineno") or getattr(cur, "end_lineno", None) is None:
            continue
        seg = src[abs_of((cur.lineno, cur.col_offset)):
                  abs_of((cur.end_lineno, cur.end_col_offset))]
        if any(m in seg for m in QSS_MARKERS):
            regions.append((abs_of((cur.lineno, cur.col_offset)),
                            abs_of((cur.end_lineno, cur.end_col_offset))))
    return regions


def _bad_qss_literals(path: pathlib.Path) -> list[str]:
    """Обычные строки с «}}» внутри QSS-выражения — это баг Qt-стилей."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:                                  # не наш файл
        return []
    regions = _qss_expression_regions(src, tree)
    if not regions:
        return []

    lines = src.split("\n")
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln) + 1

    def abs_of(pos):
        return starts[pos[0] - 1] + pos[1]

    bad = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.STRING:
            continue
        text = tok.string
        i = 0
        while i < len(text) and text[i] not in ('"', "'"):
            i += 1
        if "f" in text[:i].lower():
            continue                                     # f-строки: {{ }} верны
        if "{{" not in text and "}}" not in text:
            continue
        s, e = abs_of(tok.start), abs_of(tok.end)
        if any(rs <= s and e <= re_ for rs, re_ in regions):
            bad.append(f"{path.name}:{tok.start[0]}: {text.strip()[:70]}")
    return bad


# Каталоги, которые не являются кодом UmbraNet: служебные папки, виртуальные
# окружения (любого имени — pyvenv.cfg внутри), site-packages с чужими
# библиотеками. Без этого фильтра прогон из venv внутри репозитория
# «находил бы» чужие файлы (rich, pygments) и падал по ложному срабатыванию.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".tox", ".nox", ".mypy_cache",
              ".pytest_cache", "build", "dist", "out", "target"}


def _is_ours(path: pathlib.Path) -> bool:
    parts = set(path.parts)
    if parts & _SKIP_DIRS:
        return False
    if "site-packages" in parts:
        return False
    # виртуальное окружение любого имени: pyvenv.cfg где-то выше файла
    for parent in path.parents:
        if (parent / "pyvenv.cfg").exists():
            return False
        if parent == ROOT:
            break
    return True


def test_no_broken_qss_escaping_anywhere():
    """Ни один QSS-литерал не должен содержать лишних фигурных скобок."""
    problems = []
    for p in sorted(ROOT.rglob("*.py")):
        if not _is_ours(p):
            continue
        problems.extend(_bad_qss_literals(p))
    assert not problems, (
        "Найдены QSS-строки с лишними скобками — Qt молча выбросит все "
        "правила после них:\n  " + "\n  ".join(problems)
    )


def test_power_button_qss_is_balanced():
    """Конкретно QSS кнопки Старт/Стоп: скобки сбалансированы, есть :disabled."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.header import _idle_qss, _power_qss
    from umbranet import theme

    qss = _power_qss(bg1=theme.GREEN, bg2="#10b981", hover1="#55ffaa",
                     hover2="#20d490", pressed1="#1e8a55", pressed2="#0e7a45")
    for label, sheet in (("_power_qss", qss), ("_idle_qss", _idle_qss())):
        assert "}}" not in sheet, f"{label}: лишние скобки ломают парсинг"
        assert sheet.count("{") == sheet.count("}"), f"{label}: скобки не сбалансированы"
    assert "QPushButton:disabled" in qss, "потеряно правило серой (disabled) кнопки"


# ── 2. Защищённые процессы ───────────────────────────────────────────────────

def test_protected_processes_facade():
    import umbranet.engine_adapter as ea

    assert set(ea.protected_processes()) >= {"chrome.exe", "msedge.exe", "firefox.exe"}
    for name in ("chrome.exe", "CHROME.EXE", " msedge.exe ", "Firefox.exe"):
        assert ea.is_protected_process(name), f"{name} должен быть защищён"
    for name in ("discord.exe", "telegram.exe", "", None, "chromium.exe"):
        assert not ea.is_protected_process(name), f"{name} не должен быть защищён"


def test_manual_canvas_hides_cross_and_blocks_click():
    """У защищённой записи нет ✕: клик в зоне удаления не эмитит сигнал."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.manual_canvas import CARD_H, CARD_STRIDE, ManualCanvas

    canvas = ManualCanvas()
    canvas.resize(700, 200)
    canvas.set_items([
        {"name": "chrome.exe", "key": "routed_processes", "icon": "", "badge": "",
         "badge_color": "", "display": "chrome.exe", "protected": True},
        {"name": "Discord.exe", "key": "routed_processes", "icon": "", "badge": "",
         "badge_color": "", "display": "Discord.exe", "protected": False},
    ])
    removed = []
    canvas.itemRemoved.connect(lambda n, k: removed.append((n, k)))

    QtTest = pytest.importorskip("PySide6.QtTest")

    def click(row: int):
        pos = QtCore.QPoint(int(canvas.width() * 0.97),
                            row * CARD_STRIDE + CARD_H // 2)
        QtTest.QTest.mouseClick(canvas, QtCore.Qt.LeftButton,
                                QtCore.Qt.NoModifier, pos)

    click(0)
    assert removed == [], "защищённый chrome.exe не должен удаляться кликом по ✕"
    click(1)
    assert removed == [("Discord.exe", "routed_processes")], "обычный процесс должен удаляться"


def test_protected_tooltip_explains_lock():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from umbranet.widgets.manual_canvas import CARD_H, ManualCanvas

    canvas = ManualCanvas()
    canvas.resize(700, 200)
    canvas.set_items([
        {"name": "chrome.exe", "key": "routed_processes", "icon": "", "badge": "",
         "badge_color": "", "display": "chrome.exe", "protected": True},
    ])
    # QTest.mouseMove не доставляет событие скрытому виджету, поэтому зовём
    # обработчик напрямую — проверяем логику, а не доставку событий Qt.
    QtGui = pytest.importorskip("PySide6.QtGui")
    local = QtCore.QPointF(canvas.width() * 0.97, CARD_H // 2)
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseMove, local, canvas.mapToGlobal(local),
                           QtCore.Qt.NoButton, QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
    canvas.mouseMoveEvent(ev)
    tip = canvas.toolTip()
    assert "защит" in tip.lower(), f"тултип должен объяснять защиту, а не молчать (получено: {tip!r})"


# ── 3. Гард кнопки «Старт» ───────────────────────────────────────────────────

def test_start_button_guard_requires_targets():
    """Старт серый без целей и снова активен после выбора сервиса."""
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    import umbranet.engine_adapter as ea
    from umbranet.app import MainWindow

    win = MainWindow()
    try:
        win.engine.config["routed_domains"] = []
        win.engine.config["routed_subscriptions"] = []
        win._update_start_button()
        assert not win._can_start()
        assert not win.control.btn_power.isEnabled(), "Старт должен быть серым"

        win.engine.config["routed_domains"] = ["youtube.com"]
        win._update_start_button()
        assert win._can_start()
        assert win.control.btn_power.isEnabled(), "Старт должен стать активным"

        # процессы chrome/msedge/firefox сами по себе НЕ дают зелёный Старт
        win.engine.config["routed_domains"] = []
        win.engine.config["routed_processes"] = ["chrome.exe", "msedge.exe", "firefox.exe"]
        win._update_start_button()
        assert not win._can_start(), "процессы не должны активировать Старт"
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("remove", [False, True], ids=["add-subscription", "remove-subscription"])
def test_subscription_ui_uses_engine_edit_api(monkeypatch, remove):
    from types import SimpleNamespace
    from unittest.mock import Mock
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from umbranet import engine_adapter as adapter
    from umbranet.views.routing import RoutingView
    engine = adapter._StubEngine()
    url = "https://example.org/subscription"
    if remove:
        engine.config["routed_subscriptions"] = [url]
    engine.change_subscription = Mock(wraps=engine.change_subscription)
    edit = QtWidgets.QLineEdit()
    edit.setText(url)
    signal = Mock()
    view = SimpleNamespace(engine=engine, add_input=edit, _subscription_done=signal)
    callbacks = []
    monkeypatch.setattr(adapter, "update_subscriptions_async", callbacks.append)
    try:
        if remove:
            RoutingView._remove_subscription(view, url)
            engine.change_subscription.assert_called_once_with(url, remove=True)
        else:
            RoutingView._add_typed(view)
            engine.change_subscription.assert_called_once_with(url)
        assert engine.config["routed_subscriptions"] == ([] if remove else [url])
        assert not edit.isEnabled()
        assert len(callbacks) == 1
        callbacks[0](True, 12)
        signal.emit.assert_called_once_with(True, 12, not remove)
    finally:
        edit.deleteLater()
        app.processEvents()


def test_duplicate_subscription_does_not_become_manual_domain(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from umbranet import engine_adapter as adapter
    from umbranet.views.routing import RoutingView
    engine = adapter._StubEngine()
    url = "https://example.org/subscription"
    engine.config["routed_subscriptions"] = [url]
    before = list(engine.config["routed_domains"])
    edit = QtWidgets.QLineEdit(url)
    view = SimpleNamespace(engine=engine, add_input=edit, _apply=Mock())
    updater = Mock()
    monkeypatch.setattr(adapter, "update_subscriptions_async", updater)
    try:
        RoutingView._add_typed(view)
        assert engine.config["routed_domains"] == before
        updater.assert_not_called()
        view._apply.assert_not_called()
        assert edit.isEnabled()
    finally:
        edit.deleteLater()
        app.processEvents()


def test_generic_remove_delegates_subscriptions_to_safe_handler():
    from types import SimpleNamespace
    from unittest.mock import Mock
    pytest.importorskip("PySide6.QtWidgets")
    from umbranet.views.routing import RoutingView
    view = SimpleNamespace(_remove_subscription=Mock())
    RoutingView._remove(view, "https://example.org/list", "routed_subscriptions")
    view._remove_subscription.assert_called_once_with("https://example.org/list")
