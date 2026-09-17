"""
Тесты «Маршрутизации», «DNS-профилей» и «Настроек» в узком окне.
================================================================================

Раздел §27.5 журнала: после починки «Сети и диагностики» и «О программе» остались
три вкладки, которые на ту же болезнь не проверялись. Проверил — болезнь нашлась в
двух из трёх.

**«Настройки» (`settings.py`)** — просьба пользователя «ничего не обрезается и не
вылезает за границы» ломалась в пяти местах:

  1. подписи строк-переключателей стояли с жёсткой высотой 22 px («чтобы не считать
     heightForWidth на пиксель ресайза»), а в узком окне они переносятся на вторую
     строку и обрезались: «Маршрутизировать ВСЕ домены через профиль …», «Приоритет
     IPv6 для заблокированных сайтов…», «Optimistic cache …» — вторая строка текста
     просто исчезала;
  2. описания секций — то же самое, но по 28/32 px;
  3. заголовок «Настройки» и три кнопки («Резервная копия», «Восстановить»,
     «Сбросить») стояли одним рядом и держали минимальную ширину вкладки в 617 px:
     при окне 560 кнопки сжимались в обрезки, а заголовок превращался в «Настрой»;
  4. поле «Fallback IPv6» стояло с жёсткой шириной 240 px (`setFixedWidth`) — потолок
     вместо предела; вместе с подписью оно распирало карточку;
  5. подписи слева от полей — тоже `setFixedWidth(150)`: вместе с полем (и особенно с
     выпадающим списком, который держит 200 px) они не давали вкладке ужаться —
     в окне 560 содержимому вкладки достаётся 434 px, а минимум содержимого был
     471 px, то есть горизонтальная прокрутка (37 px) была всегда.

Итог по «Настройкам»: минимум содержимого 471 → 392 px при 434 доступных, прокрутки
нет ни на одной ширине 560–1400, вертикальные подписи переносятся и не обрезаются.
Жёсткие ширины стали пределами: подписи форм (`_FormLabel`, 150 → 110 px) и поля
(`setMaximumWidth` + `setMinimumWidth`). У выпадающих списков минимум 200 px (QSS
`min-width`) оставлен как был — это ширина самого длинного пункта; место в узком окне
освобождает подпись, а не список.

**«DNS-профили» (`profiles.py`)** — левая колонка была жёстко 280 px, а пары полей
(«Основной» / «Дополнительный») стояли в обычном ряду: в узком окне редактор профиля
уезжал за край, второе поле исчезало, вкладка требовала 659 px и получала
горизонтальную прокрутку.

**«Маршрутизация» (`routing.py`)** — проверена, поломок нет: держит порог прокрутки,
ничего не обрезается. Оставлена регрессионная проверка.

Приёмы — те же, что в §27: высота подписи становится минимумом (а не пределом),
жёсткие ширины — пределом (а не константой), ряды элементов — раскладкой-потоком
(`umbranet/widgets/flow_layout.py`, теперь общий модуль для четырёх вкладок).

Запуск: python -m pytest tests/test_three_tabs_narrow.py
"""

from __future__ import annotations

import os
import pathlib
import sys
from contextlib import suppress

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "umbranet")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import (                                             # noqa: E402
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

#: Те же ширины, что в свипах по шапке, «Логам», «Сети» и «О программе».
WIDTHS = tuple(range(560, 1401, 20))


@pytest.fixture(scope="module")
def window():
    from umbranet.app import MainWindow
    w = MainWindow()
    w.show()
    yield w
    with suppress(RuntimeError):
        w.close()


@pytest.fixture(scope="module")
def views(window):
    """Открывает по очереди три вкладки и отдаёт их вместе с окном."""
    window.sidebar.set_collapsed(True, animate=False)
    out = {}
    for key in ("routing", "profiles", "settings"):
        window._show(key)                                  # noqa: SLF001
        settle(6)
        out[key] = window._views[key]                      # noqa: SLF001
    return out


def settle(times: int = 4) -> None:
    for _ in range(times):
        APP.processEvents()


def set_width(window, width: int) -> None:
    window.resize(width, 900)
    settle()


def inner_scroll(view) -> QScrollArea | None:
    return view.findChild(QScrollArea)


def cards_of(view) -> list[QWidget]:
    """Карточки вкладки. Тип именно RoundedPanel (QFrame даёт пустой список)."""
    from umbranet.widgets.rounded_panel import RoundedPanel
    root = inner_scroll(view)
    body = root.widget() if root is not None else view
    return [c for c in body.findChildren(RoundedPanel) if c.parentWidget() is body]


def all_labels(view) -> list[QLabel]:
    root = inner_scroll(view)
    body = root.widget() if root is not None else view
    return [lb for lb in body.findChildren(QLabel) if lb.isVisible() and lb.width() > 0]


# ── 1. Ничего не обрезано ни на одной ширине ────────────────────────────────

def test_no_text_is_cut_in_three_tabs(window, views):
    """Ни одна переносящаяся подпись не обрезана на 560–1400 ни в трёх вкладках."""
    problems = []
    for key, view in views.items():
        for width in WIDTHS:
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            for lb in all_labels(view):
                if not lb.wordWrap() or not lb.text() or lb.text().startswith("<"):
                    continue
                need = lb.heightForWidth(lb.width())
                if need > lb.height() + 1:
                    problems.append(
                        f"{key} {width}: «{lb.text()[:34]}…» нужно {need}, есть {lb.height()}"
                    )
    assert not problems, "подписи обрезаны: " + "; ".join(problems[:6])


def test_toggle_labels_grow_in_narrow_window(window, views):
    """Подписи переключателей растут по тексту: в узком окне они выше, чем в широком.

    До правки высота была жёстко 22 px, и вторая строка текста пропадала целиком.
    """
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    set_width(window, 1280)
    label = view._toggle_labels["route_all"]               # noqa: SLF001
    wide = label.height()

    set_width(window, 560)
    narrow = label.height()

    assert narrow > 22, f"подпись осталась в одну строку ({narrow} px) — значит, режется"
    assert narrow >= wide, f"высота не растёт с сужением: {wide} → {narrow}"
    assert label.heightForWidth(label.width()) <= narrow + 1


def test_section_descriptions_grow(window, views):
    """Описания секций («Темы», «DNS-фильтрация») тоже занимают столько строк, сколько нужно."""
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    set_width(window, 560)
    for name in ("_theme_desc", "_filter_desc"):
        label = getattr(view, name)
        need = label.heightForWidth(label.width())
        assert need <= label.height() + 1, f"{name}: нужно {need}, есть {label.height()}"
        # И механизм: высота — минимум, а не предел. В «Темах» текст сейчас
        # укладывается в две строки, поэтому по пикселям поломка не видна; а вот
        # setFixedHeight виден всегда: у жёсткой высоты минимум равен пределу.
        assert label.minimumHeight() != label.maximumHeight(), (
            f"{name}: высота жёсткая ({label.height()} px) — длинный текст будет обрезан"
        )
        assert label.sizePolicy().verticalPolicy() != QSizePolicy.Fixed, (
            f"{name}: политика высоты Fixed — виджет не сможет вырасти"
        )


# ── 2. Ничего не вылезает и не появляется горизонтальной прокрутки ──────────

# Отдельно проверяется запас по ширине (test_settings_keeps_width_margin): вкладка
# должна не «еле влезать», а иметь 20 px свободы, иначе любая правка на волосок
# снова включает прокрутку.

def test_no_horizontal_scroll_in_three_tabs(window, views):
    """Горизонтальной прокрутки нет ни на одной ширине — кроме «Профилей», где
    она честная (две колонки) и появляется только ниже 620 px."""
    problems = []
    for key, view in views.items():
        for width in WIDTHS:
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            scroll = inner_scroll(view)
            if scroll is None:
                continue
            maximum = scroll.horizontalScrollBar().maximum()
            if maximum > 0 and not (key == "profiles" and width < 620):
                problems.append(f"{key} {width}: прокрутка {maximum} px")
    assert not problems, "; ".join(problems[:6])


def page_scroll(window, key) -> QScrollArea | None:
    """Прокрутка уровня страницы: её создаёт приложение вокруг тяжёлых вкладок."""
    page = window.stack.widget(window._pages[key])         # noqa: SLF001
    return page if isinstance(page, QScrollArea) else None


def test_settings_keeps_width_margin(window, views):
    """«Настройки» в окне 560 px оставляют запас, а не упираются в край.

    Содержимому вкладки достаётся 434 px (окно минус боковая панель и прокрутка).
    Раньше минимум содержимого был 471 px — отсюда горизонтальная прокрутка. Теперь
    он 392 px: подписи полей (до 150 px), описания секций и жёсткие ширины полей
    ужимаются. Запас в 20 px — не украшение: без него любая правка на волосок
    возвращает прокрутку (именно так её и вернул setFixedWidth у подписей).
    """
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    set_width(window, 560)
    scroll = inner_scroll(view)
    assert scroll is not None
    body = scroll.widget()
    need = body.minimumSizeHint().width()
    have = scroll.viewport().width()
    assert need + 20 <= have, f"содержимому нужно {need} px из {have} — запаса нет"
    assert scroll.horizontalScrollBar().maximum() == 0

    # Самое широкое поле («Fallback IPv6», предел 240 px) в узком окне не должно
    # стоять в своём потолке: значит, оно ужимается, а не распирает карточку.
    fb6 = view._fb6                                        # noqa: SLF001
    assert fb6.width() < fb6.maximumWidth(), (
        f"поле «Fallback IPv6» стоит в потолке ({fb6.width()} px из {fb6.maximumWidth()})"
    )


def test_profiles_fits_from_620(window, views):
    """«Профили» должны укладываться в ширину окна, начиная с 620 px.

    Вкладка обёрнута прокруткой приложением (содержимое — две колонки), но
    горизонтальной прокрутки внутри окна быть не должно: раньше левая колонка
    была жёстко 280 px, а пары полей стояли в один ряд, и вкладке требовалось 659 px.
    """
    view = views["profiles"]
    window._show("profiles")                               # noqa: SLF001
    assert view.minimumSizeHint().width() <= window.SCROLL_MIN_PAGE_W, (  # noqa: SLF001
        f"вкладке всё ещё нужно {view.minimumSizeHint().width()} px — больше порога прокрутки"
    )
    for width in (620, 700, 900, 1280):
        set_width(window, width)
        scroll = page_scroll(window, "profiles")
        assert scroll is not None, "вкладка должна быть в прокрутке приложения"
        assert scroll.horizontalScrollBar().maximum() == 0, (
            f"окно {width}: горизонтальная прокрутка "
            f"{scroll.horizontalScrollBar().maximum()} px — содержимое шире окна"
        )


def test_nothing_sticks_out_of_cards(window, views):
    """Элементы не выходят за границу своей карточки."""
    problems = []
    for key, view in views.items():
        for width in (560, 700, 900, 1280):
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            for card in cards_of(view):
                for child in card.findChildren(QWidget):
                    if not child.isVisible() or child.width() <= 0:
                        continue
                    if child.parentWidget() is None or child.parentWidget() is card:
                        continue
                    right = child.mapTo(card, child.rect().topRight()).x()
                    if right > card.width() + 1:
                        text = child.text()[:24] if hasattr(child, "text") else ""
                        problems.append(f"{key} {width}: {type(child).__name__} "
                                        f"«{text}» выходит за карточку ({right} > {card.width()})")
    assert not problems, "; ".join(problems[:6])


def test_buttons_are_not_squeezed(window, views):
    """Кнопки не сжимаются до обрезков: их ширина не меньше нужной."""
    problems = []
    for key, view in views.items():
        for width in WIDTHS:
            window._show(key)                              # noqa: SLF001
            set_width(window, width)
            for btn in view.findChildren(QPushButton):
                if not btn.isVisible() or btn.width() <= 0 or not btn.text():
                    continue
                if btn.width() + 1 < btn.sizeHint().width():
                    problems.append(f"{key} {width}: «{btn.text()[:22]}» "
                                    f"{btn.width()} < {btn.sizeHint().width()}")
    assert not problems, "; ".join(problems[:6])


# ── 3. Заголовок «Настроек»: кнопки переезжают, а не сжимаются ──────────────

def test_settings_header_buttons_move_to_second_row(window, views):
    """В узком окне кнопки уходят под заголовок, в широком стоят в одну строку."""
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    head = view._head_bar                                  # noqa: SLF001
    title = head._title                                    # noqa: SLF001
    buttons = head._buttons                                # noqa: SLF001

    set_width(window, 1280)
    same_line = [b.mapTo(view, b.rect().center()).y() for b in buttons]
    assert max(same_line) - min(same_line) <= 2, "в широком окне кнопки должны быть в один ряд"
    assert buttons[0].width() + 1 >= buttons[0].sizeHint().width()

    set_width(window, 560)
    title_bottom = title.mapTo(view, title.rect().bottomLeft()).y()
    for btn in buttons:
        btn_top = btn.mapTo(view, btn.rect().topLeft()).y()
        assert btn_top >= title_bottom - 2, "в узком окне кнопки должны быть под заголовком"
        assert btn.width() + 1 >= btn.sizeHint().width(), (
            f"кнопка «{btn.text()}» сжата до обрезка: {btn.width()} < {btn.sizeHint().width()}"
        )
    assert title.width() + 1 >= title.sizeHint().width(), "заголовок обрезан"


def test_settings_header_does_not_overlap_first_card(window, views):
    """Второй ряд кнопок не наезжает на первую карточку (высота считается по раскладке)."""
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    head = view._head_bar                                  # noqa: SLF001
    # Порядок важен: сначала широко, потом узко — проверяется именно переход
    # «один ряд → второй ряд». Если окно уже узкое, перехода нет и поломка не видна.
    for width in (1280, 560, 900, 620, 700, 560):
        set_width(window, width)
        cards = cards_of(view)
        if not cards:
            continue
        top_card = min(cards, key=lambda c: c.mapTo(view, c.rect().topLeft()).y())
        head_bottom = head.mapTo(view, head.rect().bottomLeft()).y()
        card_top = top_card.mapTo(view, top_card.rect().topLeft()).y()
        assert head_bottom <= card_top + 1, (
            f"окно {width}: кнопки заголовка перекрывают карточку "
            f"({head_bottom} > {card_top})"
        )


# ── 4. Поля ввода: ужимаются, но не исчезают ────────────────────────────────

def test_fields_shrink_but_stay_usable(window, views):
    """В узком окне поля короче, но не меньше минимума; в широком — прежняя ширина."""
    from umbranet.views.settings import _FIELD_MIN_W
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    fields = [view._port, view._fb4, view._fb6]            # noqa: SLF001

    set_width(window, 1280)
    wide = [f.width() for f in fields]
    set_width(window, 560)
    for field, wide_w in zip(fields, wide):
        least = min(field.maximumWidth(), _FIELD_MIN_W)     # у полей свои пределы ширины
        assert field.width() >= least, f"поле ужалось до {field.width()} px (< {least})"
        assert field.width() <= field.maximumWidth(), "поле шире своего предела"
    assert fields[0].width() <= max(wide), "в узком окне поле не должно быть шире, чем в широком"
    # Поля стоят на своей обычной ширине и при 560 px: ужиматься им не приходится
    # (подписи уже отдали лишнее), но и распирать карточку они не должны. Жёсткая
    # ширина у самого широкого поля («Fallback IPv6», предел 240 px) вернула бы
    # горизонтальную прокрутку — это отдельно проверяет
    # test_no_horizontal_scroll_in_three_tabs.
    for field in view.findChildren(QLineEdit):
        assert field.width() <= field.maximumWidth(), (
            f"поле шире своего предела: {field.width()} > {field.maximumWidth()}"
        )


def test_form_labels_shrink_in_narrow_window(window, views):
    """Подписи слева от полей: в широком окне ровно 150 px, в узком ужимаются.

    Было setFixedWidth(150): подпись вместе с полем держала минимальную ширину
    вкладки (в окне 560 содержимому достаётся 434 px), из-за чего появлялась
    горизонтальная прокрутка. Выравнивание подписей (150 px) сохранено.
    """
    from umbranet.views.settings import _LABEL_MAX_W, _LABEL_MIN_W
    view = views["settings"]
    window._show("settings")                               # noqa: SLF001
    texts = ("Порт", "Fallback IPv4", "Fallback IPv6", "Тема приложения")
    pick = lambda: [lb for lb in view.findChildren(QLabel) if lb.text() in texts]  # noqa: E731

    set_width(window, 1280)
    wide = {lb.text(): lb.width() for lb in pick()}
    assert set(wide) == set(texts), wide
    for text, width in wide.items():
        assert width <= _LABEL_MAX_W, f"{text}: подпись {width} px шире предела {_LABEL_MAX_W}"
    # Выравнивание сохранено: короткие подписи занимают прежние 150 px, как при
    # setFixedWidth, поэтому поля в форме стоят в одну линию.
    assert wide["Порт"] == _LABEL_MAX_W, wide
    assert wide["Тема приложения"] == _LABEL_MAX_W, wide

    for label in pick():
        # Механизм: ширина задана пределом (setMaximumWidth), а не жёстко. С
        # setFixedWidth(150) подпись вместе с полем возвращала минимальную ширину
        # всей вкладки — именно из-за этого в узком окне появлялась прокрутка.
        assert label.minimumWidth() != label.maximumWidth(), (
            f"«{label.text()}»: ширина жёсткая ({label.width()} px) — ужиматься не сможет"
        )

    set_width(window, 560)
    narrow = {lb.text(): lb.width() for lb in pick()}
    for text, width in narrow.items():
        assert _LABEL_MIN_W <= width <= _LABEL_MAX_W, f"{text}: {width} px"
    # Подписи, стоящие рядом с выпадающими списками, в узком окне реально ужались:
    # список держит 200 px, и место освободить может только подпись.
    shrunk = [lb.text() for lb in pick() if lb.width() < _LABEL_MAX_W]
    assert shrunk, f"ни одна подпись не ужалась: {narrow}"


# ── 5. «Профили»: колонка и пары полей ──────────────────────────────────────

def test_profiles_left_column_shrinks(window, views):
    """Левая колонка ужимается в узком окне и остаётся прежней в широком."""
    from umbranet.views.profiles import _LEFT_MAX_W, _LEFT_MIN_W
    view = views["profiles"]
    window._show("profiles")                               # noqa: SLF001
    set_width(window, 1280)
    assert view._left.width() == _LEFT_MAX_W, view._left.width()
    set_width(window, 560)
    narrow = view._left.width()
    assert _LEFT_MIN_W <= narrow <= _LEFT_MAX_W, narrow
    assert narrow < _LEFT_MAX_W, (
        f"в узком окне колонка не ужалась ({narrow} px при пределе {_LEFT_MAX_W}) — "
        "именно из-за этого редактор профиля уезжал за край"
    )


def test_profile_field_pairs_stack_in_narrow_window(window, views):
    """Пары полей «Основной / Дополнительный» переносятся, а не выдавливают друг друга."""
    view = views["profiles"]
    window._show("profiles")                               # noqa: SLF001
    primary, secondary = view._fields["ipv4_primary"], view._fields["ipv4_secondary"]

    set_width(window, 1280)
    assert abs(primary.mapTo(view, primary.rect().topLeft()).y()
               - secondary.mapTo(view, secondary.rect().topLeft()).y()) <= 2, \
        "в широком окне поля должны стоять рядом"

    set_width(window, 560)
    p_bottom = primary.mapTo(view, primary.rect().bottomLeft()).y()
    s_top = secondary.mapTo(view, secondary.rect().topLeft()).y()
    assert s_top >= p_bottom - 2, "в узком окне второе поле должно уйти на новую строку"


def test_long_profile_name_is_elided(window, views):
    """Длинное имя профиля укорачивается многоточием, полное — в подсказке.

    Имя ограничено 20 знаками (MAX_PROFILE_NAME_LEN), но и оно не влезает в узкую
    колонку: без укорачивания Qt обрезал бы хвост без знака, и человек не понял бы,
    что имя длиннее. Полный текст остаётся в подсказке кнопки.
    """
    from core.profile_utils import MAX_PROFILE_NAME_LEN
    from umbranet.views.profiles import _ElidedButton
    long_name = "О" * MAX_PROFILE_NAME_LEN                 # ровно предел длины

    view = views["profiles"]
    users = view.engine.config.setdefault("user_dns_profiles", [])
    profile = {"id": "test-long-name", "name": long_name,
               "ipv4_primary": "1.1.1.1", "ipv4_secondary": ""}
    users.append(profile)
    try:
        view.refresh()
        settle(4)
        buttons = [b for b in view.findChildren(_ElidedButton) if b.text() == long_name]
        assert buttons, "строка профиля не найдена"
        button = buttons[0]
        assert button.toolTip() == long_name, "полное имя потерялось из подсказки"

        # в широком окне имя влезает
        set_width(window, 1280)
        wide_col = view._left.width()                      # noqa: SLF001
        assert button.width() >= 100, f"имя сжато до {button.width()} px без нужды"

        # в узком — укорачивается, но колонка не распирается
        set_width(window, 560)
        shown = button.display_text()
        assert shown.endswith("…"), f"имя обрезано без многоточия: {shown!r}"
        assert len(shown) < len(long_name)
        assert view._left.width() <= wide_col, "колонка распирает вкладку"   # noqa: SLF001
        # Пустая пилюля пинга не должна отбирать ширину у имени: раньше у неё
        # стояла жёсткая ширина 60 px, и имя укорачивалось без всякой нужды.
        pings = [w for w in view.findChildren(QLabel) if w.text() == "" and w.width() == 60]
        assert not pings, f"пустых пилюль пинга с шириной 60 px: {len(pings)}"
    finally:
        users.remove(profile)
        view.refresh()
        settle(3)


# ── 6. «Маршрутизация»: регрессия ───────────────────────────────────────────

def test_routing_tab_keeps_its_own_scroll(window, views):
    """«Маршрутизация» держится своего правила: своя прокрутка, горизонтальной нет.

    Эта вкладка исключена из автоматической обёртки приложением
    (NEVER_WRAP_IN_PAGE_SCROLL): у неё собственная прокрутка и перетаскиваемая
    правая панель (§16.1). Проверяем, что после правок соседних вкладок правило
    не сломалось и содержимое не распирает окно вбок.
    """
    from umbranet.app import MainWindow
    assert "routing" in MainWindow.NEVER_WRAP_IN_PAGE_SCROLL

    view = views["routing"]
    window._show("routing")                                # noqa: SLF001
    for width in WIDTHS:
        set_width(window, width)
        scroll = inner_scroll(view)
        assert scroll is not None, "у вкладки должна быть своя прокрутка"
        assert scroll.horizontalScrollBar().maximum() == 0, \
            f"окно {width}: появилась горизонтальная прокрутка"
        assert scroll.verticalScrollBar().maximum() >= 0


def test_flow_layout_is_shared(window, views):
    """Раскладка-поток живёт в общем модуле и используется всеми четырьмя вкладками."""
    from umbranet.widgets.flow_layout import FlowLayout
    from umbranet.views.network import _FlowLayout as net_flow
    assert net_flow is FlowLayout, "в «Сети» должна использоваться общая раскладка"

    for key in ("profiles", "settings"):
        found = views[key].findChildren(FlowLayout)
        assert found, f"вкладка {key} не использует переносящуюся раскладку"


def test_flow_layout_wraps_and_reports_height():
    """Прямой тест раскладки: элементы переносятся, и высота это учитывает."""
    from umbranet.widgets.flow_layout import FlowLayout
    holder = QWidget()
    flow = FlowLayout(spacing=10)
    holder.setLayout(flow)
    for _ in range(4):
        btn = QPushButton("кнопка")
        btn.setFixedSize(120, 36)
        flow.addWidget(btn)
    holder.resize(500, 200)
    settle(3)

    assert flow.hasHeightForWidth(), (
        "раскладка не сообщает, что высота зависит от ширины: чужой родитель "
        "выделит ей одну строку, и вторая строка окажется под карточкой"
    )
    one_row = flow.heightForWidth(600)
    two_rows = flow.heightForWidth(300)
    assert one_row < two_rows, f"перенос не учитывается в высоте: {one_row} vs {two_rows}"
    xs = [it.geometry().x() for it in (flow.itemAt(i) for i in range(flow.count()))]
    assert max(xs) + 120 <= 500 + 1, "элемент вышел за пределы раскладки"
