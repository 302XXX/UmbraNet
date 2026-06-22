"""
UmbraNet - дизайн-токены и хелперы стилей (PySide6 / Qt Widgets).

Единый источник цветов и стилевых помощников. Все виджеты импортируют
отсюда, чтобы менять тему в одном месте.

Стиль: vibrant gradient, тёмный фон, фиолет->синий->бирюза, скругления,
мягкие тени-свечения.
"""

from __future__ import annotations

import json
import os

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

# ── Палитра ──────────────────────────────────────────────────────────────────
BG       = "#06060c"   # глубокий космический вакуум
SIDEBAR  = "rgba(13, 13, 20, 0.75)"   # полупрозрачный графитовый сайдбар (75% плотности)
CARD     = "rgba(18, 18, 30, 0.65)"   # матовое стекло (65% плотности)
CARD_TOP = "rgba(28, 28, 48, 0.40)"   # верх градиента матовых карточек (эффект блика)
CARD_DARK = "rgba(10, 10, 20, 0.50)"  # тёмное стекло («книжка» маршрутов)
ROW_BG   = "rgba(18, 18, 30, 0.55)"   # фон строки внутри тёмной карточки
BORDER   = "rgba(255, 255, 255, 0.08)" # Мягкая белая рамка (эффект преломления света)
INPUT_BG = "rgba(10, 10, 18, 0.60)"   # глубокий тёмный прозрачный инпут

ACCENT   = "#8b6dff"   # основной акцент (фиолетовый, ярче)
ACCENT2  = "#5b9bff"   # вторичный (синий, ярче)
ACCENT3  = "#34dcf0"   # бирюзовый (ярче)

GREEN    = "#3ee089"   # ок / работает
RED      = "#ff6478"   # ошибка / стоп
YELLOW   = "#fbbf24"   # предупреждение
ORANGE   = "#fb923c"   # перезапуск
PINK     = "#f259b0"   # медиа

TEXT     = "#edeef5"   # основной текст (ярче)
SUBTEXT  = "#9b9cbd"   # вторичный (ярче)
MUTED    = "#6b6c8f"   # приглушённый
WHITE    = "#ffffff"

# ── Темы ─────────────────────────────────────────────────────────────────────
# Темы загружаются динамически из папки themes/ в корне проекта.
# Любой пользователь может добавить файл-тему .json или удалить его.

DEFAULT_THEME = "neon"
CURRENT_THEME = DEFAULT_THEME


def _ui_state_file() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "umbranet_ui.json"))


def _get_themes_dir() -> str:
    themes_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "themes"))
    os.makedirs(themes_dir, exist_ok=True)
    return themes_dir


def _load_all_themes() -> dict:
    fallback_neon = {
        "label": "Umbra Neon",
        "BG": "#06060c", "SIDEBAR": "rgba(13, 13, 20, 0.75)",
        "CARD": "rgba(18, 18, 30, 0.65)", "CARD_TOP": "rgba(28, 28, 48, 0.40)",
        "CARD_DARK": "rgba(10, 10, 20, 0.50)", "ROW_BG": "rgba(18, 18, 30, 0.55)",
        "BORDER": "rgba(255, 255, 255, 0.08)", "INPUT_BG": "rgba(10, 10, 18, 0.60)",
        "ACCENT": "#8b6dff", "ACCENT2": "#5b9bff", "ACCENT3": "#34dcf0",
        "GREEN": "#3ee089", "RED": "#ff6478", "YELLOW": "#fbbf24",
        "ORANGE": "#fb923c", "PINK": "#f259b0",
        "TEXT": "#edeef5", "SUBTEXT": "#9b9cbd", "MUTED": "#6b6c8f", "WHITE": "#ffffff",
    }
    themes_dir = _get_themes_dir()
    
    # Записываем стандартную тему "neon" как шаблон для пользователя, если папка пуста
    default_neon_path = os.path.join(themes_dir, "neon.json")
    if not os.path.exists(default_neon_path):
        try:
            with open(default_neon_path, "w", encoding="utf-8") as f:
                json.dump(fallback_neon, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    loaded_themes = {}
    if os.path.exists(themes_dir):
        for fname in os.listdir(themes_dir):
            if fname.endswith(".json"):
                theme_id = fname[:-5]
                path = os.path.join(themes_dir, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict) and "label" in data:
                            loaded_themes[theme_id] = data
                except Exception:
                    pass

    if not loaded_themes:
        loaded_themes["neon"] = fallback_neon
    return loaded_themes


THEMES = _load_all_themes()


def _load_theme_name() -> str:
    try:
        with open(_ui_state_file(), "r", encoding="utf-8") as f:
            name = (json.load(f) or {}).get("theme", DEFAULT_THEME)
        return name if name in THEMES else DEFAULT_THEME
    except Exception:
        return DEFAULT_THEME


def theme_label(name: str | None = None) -> str:
    name = name or CURRENT_THEME
    return THEMES.get(name, THEMES[DEFAULT_THEME]).get("label", name)


def theme_items() -> list[tuple[str, str]]:
    return [(key, data.get("label", key)) for key, data in THEMES.items()]


def _rebuild_modes() -> None:
    global MODES, BACKEND_TO_UI
    MODES = {
        "blue":  {"name": "DNS Only", "emoji": "⚙", "c1": ACCENT,   "c2": ACCENT2,   "backend": "off"},
        "black": {"name": "Combo",    "emoji": "⚡", "c1": "#6366f1", "c2": "#a855f7", "backend": "combo"},
        "red":   {"name": "DPI Only", "emoji": "🛡", "c1": RED,      "c2": "#f59e0b", "backend": "zapret"},
    }
    BACKEND_TO_UI = {v["backend"]: k for k, v in MODES.items()}


def apply_theme(name: str) -> str:
    """Применяет палитру к токенам темы. Существующие виджеты надо перестроить."""
    global CURRENT_THEME, BG, SIDEBAR, CARD, CARD_TOP, CARD_DARK, ROW_BG, BORDER, INPUT_BG
    global ACCENT, ACCENT2, ACCENT3, GREEN, RED, YELLOW, ORANGE, PINK, TEXT, SUBTEXT, MUTED, WHITE
    name = name if name in THEMES else DEFAULT_THEME
    data = THEMES[name]
    for key, value in data.items():
        if key == "label":
            continue
        globals()[key] = value
    CURRENT_THEME = name
    if "MODES" in globals():
        _rebuild_modes()
    return name


def save_theme_preference(name: str) -> str:
    name = apply_theme(name)
    path = _ui_state_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    state["theme"] = name
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return name


apply_theme(_load_theme_name())

APP_NAME = "UmbraNet"

# ── Размеры ──────────────────────────────────────────────────────────────────
WIN_W = 1180
WIN_H = 760
WIN_MIN_W = 1100
WIN_MIN_H = 680

SIDEBAR_W_EXPANDED = 210
SIDEBAR_W_COLLAPSED = 68

# ── Режимы DPI (см. blueprint, раздел 4) ─────────────────────────────────────
_rebuild_modes()


# ── Хелперы стилей ───────────────────────────────────────────────────────────
def grad(c1: str, c2: str, horizontal: bool = True) -> str:
    """QSS-строка линейного градиента."""
    if horizontal:
        return f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c1}, stop:1 {c2})"
    return f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c1}, stop:1 {c2})"


def brand_grad(horizontal: bool = True) -> str:
    """Главный брендовый градиент (фиолет->синий)."""
    return grad(ACCENT, ACCENT2, horizontal)


def glow(widget: QWidget, color: str, blur: int = 22, dy: int = 6, alpha: int = 150) -> QWidget:
    """Добавляет цветное свечение (тень) под виджетом. Возвращает тот же виджет."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    c = QColor(color)
    c.setAlpha(alpha)
    effect.setColor(c)
    effect.setOffset(0, dy)
    widget.setGraphicsEffect(effect)
    return widget


def card_qss(radius: int = 16, border: bool = True) -> str:
    """QSS для карточки с вертикальным градиентом и скруглением."""
    b = f"border:1px solid {BORDER};" if border else "border:none;"
    return (
        f"background:{grad(CARD_TOP, CARD, horizontal=False)};"
        f"{b}border-radius:{radius}px;"
    )


def scrollbar_qss() -> str:
    """QSS для аккуратного тонкого вертикального скроллбара."""
    return (
        f"QScrollBar:vertical{{background:transparent;width:10px;margin:2px;}}"
        f"QScrollBar::handle:vertical{{background:{BORDER};border-radius:5px;min-height:30px;}}"
        f"QScrollBar::handle:vertical:hover{{background:{ACCENT};}}"
        f"QScrollBar::handle:vertical:disabled{{background:transparent;}}"
        f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        f"QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}"
        f"QScrollBar:horizontal{{background:transparent;height:10px;margin:2px;}}"
        f"QScrollBar::handle:horizontal{{background:{BORDER};border-radius:5px;min-width:30px;}}"
        f"QScrollBar::handle:horizontal:hover{{background:{ACCENT};}}"
        f"QScrollBar::handle:horizontal:disabled{{background:transparent;}}"
        f"QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{{width:0;}}"
        f"QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{{background:transparent;}}"
    )
