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


MAX_TEST_ID_LENGTH = 512


def pytest_collection_modifyitems(items):
    """Fail before verbose reporting can print a multi-megabyte parameter ID.

    Keep the full payload in the test; use pytest.param(..., id="over-5MiB")
    instead of letting pytest derive a name from that payload. The diagnostic
    itself is bounded, including when several such parameters were collected.
    """
    oversized = [item for item in items if len(item.nodeid) > MAX_TEST_ID_LENGTH]
    if oversized:
        details = "; ".join(
            f"{item.nodeid[:120]}... ({len(item.nodeid)} characters)"
            for item in oversized[:3]
        )
        raise pytest.UsageError(
            f"{len(oversized)} test IDs exceed {MAX_TEST_ID_LENGTH} characters. "
            f"Add short explicit pytest.param(..., id=...) names. {details}"
        )


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


# ── Qt-среда для тестов: детерминированный шрифт + защита от сегфолта ───────
#
# Всё выполняется в pytest_configure — ДО импорта тестовых модулей: они
# создают QApplication ещё на уровне модуля
# (`APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])`),
# а фикстуры к этому моменту ещё не запускаются.
#
# 1) Шрифт.
# Тесты интерфейса меряют пиксели: ширина вкладок, переносы текста, появление
# прокрутки. Метрики зависят от шрифта, а по умолчанию Qt берёт системный:
# на Linux это «Sans Serif» (DejaVu Sans), на Windows — Segoe UI. Метрики
# разные, и одни и те же тесты дают разный результат на разных ОС (на Windows
# тексты и эмодзи шире → вкладка «не влезала» в узкое окно).
#
# Поэтому шрифт приложения фиксируем на «UmbraNetTestSans» 9 pt — это DejaVu
# Sans 2.37 из репозитория (tests/assets), переименованный в своё семейство,
# чтобы не зависеть от того, что установлено в системе. Эмодзи в нём замаплены
# на .notdef-глиф: их ширина одинакова на всех ОС и совпадает с тем, что даёт
# Linux-раннер (там нет эмодзи-шрифта, и Qt рисует тот же .notdef). Без этого
# на Windows Qt подставлял бы Segoe UI Emoji — и эмодзи оказывались на
# несколько пикселей шире. Размер 9 pt — стандартный дефолт Qt (тот же, что и
# сейчас на Linux-раннерах, где тесты зелёные).
#
# 2) Сегфолт (exit 139) в середине прогона.
# Тесты создают и закрывают MainWindow по несколько штук за прогон. Обёртки
# QScreen «прилипают» к зомби-окнам и образуют циклы ссылок, видимые только
# циклическому GC. Когда GC собирает такой цикл, освобождается обёртка экрана,
# и Shiboken уничтожает C++ QScreen — единственный экран offscreen-платформы.
# Список экранов приложения остаётся висящим, и следующее создание виджета
# (QWidget → QFont → QWidget::screen → QGuiApplication::screenAt) сегфолит.
# Место падения разное каждый раз (в win_shell, в network, в dialog) — потому
# что падает не «тот» тест, а первый виджет, созданный после.
#
# Лечится связкой (до — 139 на CI, после — стабильно зелёный прогон):
#  • пин: сильные ссылки на QApplication и обёртки экранов живы весь прогон
#    (фикстура _pin_qt_screens ниже) — обёртки не dealloc'ятся;
#  • gc.disable(): циклический сборщик больше не проходит циклы зомби-окон,
#    поэтому не освобождает и те обёртки, что пин не успел увидеть (они
#    создаются в течение теста, между фикстурами). Память при этом не растёт
#    (замер: ~400 MB на весь прогон), и ни один тест не зависит от
#    циклического GC (weakref/gc в тестах не используются).
_TEST_FONT_FAMILY = "UmbraNetTestSans"

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


def _prepare_qt_environment() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        import gc
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QFont, QFontDatabase
    except Exception:                                    # noqa: BLE001 — нет PySide6
        return
    # QApplication нужен ещё до доступа к QFontDatabase (без него Qt ругается:
    # «Must construct a QGuiApplication before accessing QFontDatabase»).
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    # 1) фиксированный шрифт (см. комментарий выше)
    for name in ("umbra_test_sans.ttf", "umbra_test_sans_bold.ttf"):
        ttf = os.path.join(os.path.dirname(__file__), "assets", name)
        if os.path.exists(ttf):
            try:
                QFontDatabase.addApplicationFont(ttf)
            except Exception:                            # noqa: BLE001
                pass
    if _TEST_FONT_FAMILY in QFontDatabase.families():
        current = app.font()
        if current.family() != _TEST_FONT_FAMILY or abs(current.pointSizeF() - 9.0) > 0.01:
            font = QFont(_TEST_FONT_FAMILY)
            font.setPointSizeF(9.0)
            app.setFont(font)
    # 2) защита от сегфолта: пин + выключение циклического GC
    _pin_qt_objects()
    gc.disable()


def pytest_configure(config):
    _prepare_qt_environment()


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


@pytest.fixture(autouse=True)
def _offline_gui_background_updates(monkeypatch):
    """GUI geometry tests must not launch production update requests/timers.

    Only GUI entry points are disabled. Scheduler, download and release-checker
    tests exercise the real core methods with their own fake clocks/network.
    Manual UI update controls are separately tested with a fake checker.
    """
    try:
        from umbranet.app import MainWindow
    except ImportError:  # headless test environment without Qt system libraries
        return
    monkeypatch.setattr(MainWindow, "_start_background_updates", lambda self: None)
    monkeypatch.setattr(MainWindow, "_check_program_updates", lambda self: None)
