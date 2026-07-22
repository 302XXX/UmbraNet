"""
UmbraNet - горячие клавиши копирования/вставки для русской раскладки.

Проблема: при активной русской раскладке Ctrl+C/V/X/A не срабатывают, потому
что физические клавиши C/V/X/A вводят кириллические символы (С/М/Ч/Ф), и
многие виджеты не распознают стандартные сочетания.

Решение: глобальный фильтр событий на уровне QApplication. Он ловит KeyPress
с зажатым Ctrl и определяет нажатую клавишу ДВУМЯ способами:
  1) по nativeVirtualKey (надёжно на Windows — VK-код не зависит от раскладки);
  2) по введённому кириллическому символу (fallback, в т.ч. для Linux).
При совпадении вызывает стандартное действие активного поля ввода.

Подключение (один раз):
    from umbranet.ru_clipboard import install_ru_clipboard
    install_ru_clipboard(app)
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit

# Windows Virtual-Key коды (раскладко-независимые)
_VK = {"C": 0x43, "V": 0x56, "X": 0x58, "A": 0x41}

# Кириллические символы на тех же физических клавишах (fallback)
_CYR = {"с": "C", "м": "V", "ч": "X", "ф": "A"}

# поддерживаемые поля ввода и их действия
_ACTIONS = {
    "C": lambda w: w.copy(),
    "V": lambda w: w.paste(),
    "X": lambda w: w.cut(),
    "A": lambda w: w.selectAll(),
}


def _resolve_key(event) -> str | None:
    """Определяет логическую клавишу (C/V/X/A) независимо от раскладки."""
    # 1) по нативному VK-коду (Windows)
    try:
        vk = event.nativeVirtualKey()
        for letter, code in _VK.items():
            if vk == code:
                return letter
    except Exception:
        pass
    # 2) по введённому символу — латиница ИЛИ кириллица
    text = (event.text() or "").lower()
    if text:
        if text in _CYR:
            return _CYR[text]
        up = text.upper()
        if up in _ACTIONS:
            return up
    return None


class _RuClipboardFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() != QEvent.KeyPress:
            return False
        mods = event.modifiers()
        # нужен именно Ctrl (но не Ctrl+Alt = AltGr и т.п.)
        if not (mods & Qt.ControlModifier):
            return False
        if mods & Qt.AltModifier:
            return False

        key = _resolve_key(event)
        if key is None:
            return False

        # действуем только если фокус на поле ввода
        w = QApplication.focusWidget()
        if not isinstance(w, (QLineEdit, QPlainTextEdit, QTextEdit)):
            return False

        # для readonly-полей вставку/вырезание пропускаем
        if key in ("V", "X"):
            try:
                if w.isReadOnly():
                    return False
            except Exception:
                pass

        try:
            _ACTIONS[key](w)
            return True  # событие обработано — стандартный обработчик не нужен
        except Exception:
            return False


_filter_instance = None


def install_ru_clipboard(app: QApplication) -> None:
    """Установить глобальный фильтр копипасты для русской раскладки."""
    global _filter_instance
    if _filter_instance is not None:
        return
    _filter_instance = _RuClipboardFilter(app)
    app.installEventFilter(_filter_instance)
