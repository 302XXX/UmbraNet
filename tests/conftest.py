"""Pytest fixtures — добавляем core в sys.path как делает engine_adapter."""
import os
import sys

# Корень проекта
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CORE = os.path.join(ROOT, "core")
DNS = os.path.join(CORE, "dns")
DPI = os.path.join(CORE, "dpi")

for p in (CORE, DNS, DPI, os.path.join(DPI, "ai_strategy"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_geo_cache_session(tmp_path_factory):
    """Путь кэша гео подменяется ОДИН раз на весь прогон, а не на каждый тест.

    Кэш гео-точек (Map/core/geo.py) пишет сама программа. Запись идёт из фонового
    воркера, то есть может случиться в любой момент — в том числе между тестами,
    когда подмена уже снята. Тогда в корне проекта появлялся пустой
    `geo_cache.json`. Переменная среды живёт весь прогон, поэтому наружу не
    просачивается ничего. Плюс в самом модуле гео пустой снимок не пишется.
    """
    cache_dir = tmp_path_factory.mktemp("geo_cache")
    os.environ["UMBRANET_GEO_CACHE"] = str(cache_dir / "geo_cache.json")
    try:
        import geo
        geo.set_cache_path(None)                  # путь возьмётся из переменной среды
    except ImportError:
        pass
    yield cache_dir
    os.environ.pop("UMBRANET_GEO_CACHE", None)
    try:
        import geo
        geo.set_cache_path(None)
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _isolated_geo_cache(tmp_path_factory):
    """Каждому тесту — свой файл кэша (чтобы тесты не влияли друг на друга)."""
    cache_path = tmp_path_factory.mktemp("geo_cache_test") / "geo_cache.json"
    try:
        import geo
    except ImportError:                            # карты нет в сборке — тестам она не нужна
        yield cache_path
        return
    geo.set_cache_path(str(cache_path))
    yield cache_path
    geo.set_cache_path(None)                       # обратно на «сессионный» путь


@pytest.fixture(scope="session", autouse=True)
def _isolated_config_session(tmp_path_factory):
    """Прогон тестов не должен переписывать рабочий config.json проекта.

    Конфиг читает и узел DNS (`DNSServerController.__init__` → `load_config()`),
    а при чтении файл может быть пересохранён: нормализация значений и миграции
    схемы. В тестах это означало бы правку рабочей копии — например, запись
    `config_version` в файл, который лежит в git. Переменная среды живёт весь
    прогон, путь читается при каждом обращении (см. `config_file()`).
    """
    cfg_dir = tmp_path_factory.mktemp("config")
    os.environ["UMBRANET_CONFIG"] = str(cfg_dir / "config.json")
    yield cfg_dir
    os.environ.pop("UMBRANET_CONFIG", None)


# ── Пин-фикстура Qt: экраны и приложение живы весь прогон ───────────────────
#
# Почему это нужно (найдено при отладке падения CI, сентябрь 2026):
# тесты создают и закрывают MainWindow по несколько штук за прогон. Каждое
# окно при построении обращается к экрану (`self.screen()`, `primaryScreen()`),
# и PySide6-обёртка QScreen «прилипает» к C++-части зомби-окна (через
# сигнальные соединения/оконные структуры). Вместе с циклом ссылок это делает
# обёртку видимой только циклическому GC: когда сборщик проходит цикл (триггер
# — любое выделение памяти, включая внутренние импорты), обёртка dealloc'ится,
# и Shiboken при этом УНИЧТОЖАЕТ C++ QScreen — единственный экран offscreen-
# платформы. Дальше список экранов приложения висит в воздухе, и следующее
# создание виджета (QWidget → QFont → QWidget::screen → QGuiApplication::
# screenAt) сегфолит либо уходит в вечный цикл. На CI это выглядело как
# «Тесты: exit code 139» на Linux.
#
# Лечится сильным захватом (пином) обёрток: пока кто-то держит их, ни один GC
# не dealloc'ит их и не сможет удалить C++-экран. Экран и так живёт всю жизнь
# приложения — пин не меняет поведения, только убирает неопределённость.
_PINS: list = []


def _pin_qt_objects() -> None:
    try:
        from PySide6.QtGui import QGuiApplication
    except Exception:                                    # noqa: BLE001 — PySide6 может не быть
        return
    app = QGuiApplication.instance()
    if app is None:
        return
    if app not in _PINS:
        _PINS.append(app)
    try:
        for screen in QGuiApplication.screens():
            if screen not in _PINS:
                _PINS.append(screen)
    except Exception:                                    # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _pin_qt_screens():
    """Держим QApplication и QScreen в живых, чтобы GC не удалял C++-экраны."""
    _pin_qt_objects()
    yield


@pytest.fixture(autouse=True)
def _isolated_ui_state(tmp_path_factory, monkeypatch):
    """Тесты не должны трогать рабочий umbranet_ui.json проекта.

    Файл состояния UI пишут и тема, и интерфейс (теперь ещё и размер окна).
    Без этой подмены прогон тестов менял бы настройки рабочей копии:
    например, выставлял размер окна, под которым тесты запускались.
    """
    state_path = tmp_path_factory.mktemp("ui_state") / "umbranet_ui.json"
    monkeypatch.setenv("UMBRANET_UI_STATE", str(state_path))
    try:
        import ui_state
        ui_state.set_state_path(None)          # сбрасываем возможное переопределение
    except Exception:
        pass
    yield state_path
    try:
        import ui_state
        ui_state.set_state_path(None)
    except Exception:
        pass
