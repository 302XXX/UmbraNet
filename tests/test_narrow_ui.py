"""
Тесты узкого окна: кнопка «Старт» в шапке и карточки-счётчики в «Логах».
================================================================================

Две жалобы пользователя по факту работы (16.09.2026):

  1. «Разворачиваю боковую панель — кнопку Старт съедает край окна». Причина:
     разворот панели отнимает у шапки ~140 px, но ужим подписей считался только
     по resizeEvent окна — после разворота панели подписи оставались в прежнем
     (широком) виде, и «Старт» уезжал за границу. Плюс сам расчёт нужной ширины
     делался по метрикам шрифта, которые на эмодзи и QSS-отступах занижали
     ширину на десятки пикселей (замерено: 301 против 315 на кнопках режимов и
     102 против 140 у «Старта» с его минимальной шириной).

  2. «Во вкладке „Логи“ квадратики со счётчиками съедаются окном». Причина:
     строка фильтров держала минимальную ширину вкладки (≈780 px), вкладка
     оборачивалась в горизонтальную прокрутку, карточки делили её ширину и
     уезжали под край.

Проверяем новое поведение: кнопка «Старт» ужимается так же, как соседний
«Перезапуск» (подпись короче → значок), карточки ужимаются до «смайлик + цифра»,
подписи фильтров — до значков, и ни один элемент не выходит за границу окна.

Запуск: python -m pytest tests/test_narrow_ui.py
"""

from __future__ import annotations

import itertools
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
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QScrollArea

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# Окно создаётся ОДИН раз на модуль — как в tests/test_window_size.py:
# MainWindow поднимает движок и таймеры, а несколько окон подряд в одном процессе
# PySide6 приводят к падению при сборке мусора.
@pytest.fixture(scope="module")
def window():
    from umbranet.app import MainWindow
    w = MainWindow()
    w.show()
    yield w
    with suppress(RuntimeError):        # объект мог быть уже удалён Qt
        w.close()


def settle(window, times: int = 4) -> None:
    """Даёт раскладке доехать: без этого замеры идут по прежнему кадру."""
    for _ in range(times):
        APP.processEvents()
        row = getattr(window, "_topbar_row", None)
        if row is not None:
            row.invalidate()
            row.activate()


def set_width(window, width: int, panel_expanded: bool = False) -> None:
    window.sidebar.set_collapsed(not panel_expanded, animate=False)
    window.resize(width, window.height())
    settle(window)


# ── 1. Кнопка «Старт» не уезжает за край ────────────────────────────────────

def test_start_button_stays_inside_window_when_sidebar_expands(window):
    """ГЛАВНЫЙ тест жалобы №1: разворот панели не выталкивает «Старт» за окно.

    До правки: при узком окне разворот панели отнимал у шапки ~140 px, ужим не
    пересчитывался, и правый край кнопки оказывался за границей окна (замерено:
    740 при ширине окна 720) — нажать на «Старт» было нельзя.
    """
    window.resize(560, window.height())
    window.sidebar.set_collapsed(True, animate=False)          # узко И панель свёрнута
    settle(window)

    window.sidebar.set_collapsed(False, animate=False)         # человек развернул панель
    settle(window)

    btn = window.control.btn_power
    left = btn.mapTo(window, QPoint(0, 0)).x()
    assert left + btn.width() <= window.width(), (
        f"кнопка «Старт» вышла за окно: край {left + btn.width()} при ширине {window.width()}"
    )
    assert btn.isVisible(), "кнопка «Старт» должна оставаться видимой"


def test_topbar_recalculates_on_sidebar_toggle(window):
    """Шапка пересчитывает ужим при развороте панели, а не только при resize окна.

    Окно при этом не меняем — меняется только ширина боковой панели.
    """
    # 640 px — ширина, на которой свёрнутая панель оставляет шапке место для
    # частичной подписи режимов (замерено: ужим 0.67), а развёрнутая уже нет.
    set_width(window, 640, panel_expanded=False)
    before = (window.mode_switch.compression(), window.control._compact_level)
    assert before[1] == "no_status", f"подготовка теста: уровень {before[1]!r}"

    window.sidebar.set_collapsed(False, animate=False)      # человек развернул панель
    settle(window)

    after = (window.mode_switch.compression(), window.control._compact_level)
    assert after[0] > before[0], (
        f"после разворота панели ужим подписей режимов не изменился: {before} → {after}"
    )
    levels = {"full": 0, "no_status": 1, "restart_icon": 2, "power_icon": 3}
    assert levels.get(after[1], 0) >= levels.get(before[1], 0), (
        f"после разворота панели шапка не ужалась: {before} → {after}"
    )
    btn = window.control.btn_power
    left = btn.mapTo(window, QPoint(0, 0)).x()
    assert left + btn.width() <= window.width(), "«Старт» вышел за окно после разворота панели"


def test_topbar_never_overflows_in_width_sweep(window):
    """Ни на одной ширине шапка не требует больше места, чем у неё есть.

    Порог берём с допуском в 1 px: раскладка округляет доли пикселя.
    """
    overflow = []
    for panel in (False, True):
        for width in (560, 600, 640, 680, 720, 760, 800):
            set_width(window, width, panel_expanded=panel)
            row = window._topbar_row
            need = row.sizeHint().width()
            avail = window._topbar_bar.width() - 48 - row.spacing()
            if need > avail + 1:
                overflow.append(f"панель={'да' if panel else 'нет'} окно={width}: "
                                f"нужно {need}, есть {avail}")
    assert not overflow, "шапка не помещается: " + "; ".join(overflow)


def test_topbar_state_changes_monotonically(window):
    """Чем шире окно — тем меньше ужим: состояние не «мигает» на разных ширинах.

    До правки порядок уступок расходился: при 800 px подпись «Перезапуска»
    пропадала, а при 820 px возвращалась (дробный остаток в 0.1 px отправлял
    шапку на следующую ступень ужима).
    """
    states = []
    for width in range(560, 801, 20):
        set_width(window, width, panel_expanded=True)
        states.append((width,
                       round(window.mode_switch.compression(), 2),
                       window.control._compact_level,
                       round(window.control.power_compression(), 2)))

    def rank(state):
        _, mode_t, level, power_t = state
        level_rank = {"full": 0, "no_status": 1, "restart_icon": 2}.get(level, 0)
        return (level_rank, mode_t + power_t)

    for prev, cur in itertools.pairwise(states):
        prev_rank, cur_rank = rank(prev), rank(cur)
        weaker = (cur_rank[0] > prev_rank[0] or
                  (cur_rank[0] == prev_rank[0] and cur_rank[1] > prev_rank[1] + 1e-6))
        assert not weaker, f"с расширением окна ужим усилился: {prev} → {cur}"


def test_start_button_compresses_like_restart(window):
    """В узком окне «Старт» ужимается подписями и в пределе остаётся значок.

    Пользователь: «сделай так, чтобы кнопка старт не съедалась, а уменьшалась как
    кнопка перезапуска». Проверяем, что ширина «Старта» уменьшается вместе с окном
    и что при самом узком окне подпись уже не занимает места.
    """
    set_width(window, 800, panel_expanded=False)
    wide_width = window.control.btn_power.width()

    set_width(window, 560, panel_expanded=True)
    narrow_width = window.control.btn_power.width()

    assert narrow_width < wide_width, (
        f"кнопка «Старт» не ужимается: {wide_width} → {narrow_width}"
    )
    assert window.control.power_compression() > 0, "кнопка «Старт» ужата не была"
    assert window.control.btn_power.text().strip() in ("▶", "▶  Ст…", "▶  Ста…"), (
        f"в узком окне ожидали короткую подпись, получили {window.control.btn_power.text()!r}"
    )


def test_compressed_start_button_explains_itself(window):
    """У ужатой кнопки остаётся подсказка с названием действия."""
    set_width(window, 560, panel_expanded=True)
    tip = window.control.btn_power.toolTip()
    assert "Старт" in tip or "Стоп" in tip or "Запуск" in tip, (
        f"подсказка не объясняет ужатую кнопку: {tip!r}"
    )


def test_mode_switch_widths_come_from_qt(window):
    """Ширина кнопок режимов берётся у Qt, а не из метрик шрифта.

    Метрики занижали её на 14 px (301 против 315) — из-за этого шапка считала,
    что помещается, и обрезала крайнюю кнопку.
    """
    set_width(window, 800, panel_expanded=False)
    window.mode_switch.set_compression(0.0)
    settle(window)

    real = sum(btn.sizeHint().width() for btn in window.mode_switch._buttons.values())
    real += 6 * (len(window.mode_switch._buttons) - 1)
    counted = window.mode_switch.required_widths()["full"]

    assert counted == real, f"расчётная ширина режимов {counted} ≠ фактической {real}"


def test_power_width_accounts_for_minimum(window):
    """Ширина «Старта» считается с учётом его минимальной ширины (140 px).

    Кнопка-пиллюля не бывает у́же 140 px, а метрики текста давали ~102: шапка
    «экономила» 38 px, которых у неё нет.
    """
    set_width(window, 800, panel_expanded=False)
    window.control.set_compression(0.0)
    window.control.set_compact("full")
    settle(window)

    power_full, _power_icon = window.control._power_widths()
    assert power_full >= window.control._POWER_MIN_W, (
        f"ширина «Старта» в расчёте шапки меньше её минимума: {power_full}"
    )
    # и это же число участвует в общем расчёте строки
    widths = window.control.required_widths()
    gap = window.control.layout().spacing()
    assert widths["full"] - widths["no_status"] == window.control._measure_status() + gap, (
        "разница «полный/без статуса» должна быть шириной текста статуса плюс зазор"
    )


# ── 2. Карточки-счётчики в «Логах» ──────────────────────────────────────────

@pytest.fixture(scope="module")
def log_view(window):
    window._show("log")
    settle(window)
    return window._views["log"]


def stat_cards(view):
    return list(view._stat_cards)


def test_log_page_can_shrink_with_window(window, log_view):
    """Вкладка «Логи» ужимается вместе с окном, а не уезжает в горизонтальную прокрутку.

    Пока строка фильтров требовала ≈780 px, вкладка оборачивалась в прокрутку:
    её ширина не менялась, и карточки ужимались не от размера окна, а от этой
    фиксированной ширины — то есть «съедались окном».
    """
    window._show("log")
    set_width(window, 640, panel_expanded=False)

    page = window.stack.currentWidget()
    assert not isinstance(page, QScrollArea), "вкладка «Логи» ушла в горизонтальную прокрутку"
    assert log_view.minimumSizeHint().width() <= window.SCROLL_MIN_PAGE_W, (
        f"вкладка требует {log_view.minimumSizeHint().width()} px "
        f"при пороге прокрутки {window.SCROLL_MIN_PAGE_W}"
    )


def test_stat_cards_compress_to_emoji_and_number(window, log_view):
    """ГЛАВНЫЙ тест жалобы №2: в узком окне остаётся «смайлик + цифра»."""
    set_width(window, 820, panel_expanded=False)
    log_view._apply_layout_mode(force=True)
    settle(window)
    wide = {c.width() for c in stat_cards(log_view)}
    assert all(c._label.isVisible() for c in stat_cards(log_view)), "в широком окне подписи должны быть"

    set_width(window, 580, panel_expanded=True)
    log_view._apply_layout_mode(force=True)
    settle(window)

    for card in stat_cards(log_view):
        assert card.width() < max(wide), f"карточка не ужалась: {card.width()}"
        assert not card._label.isVisible(), "подпись должна уступать место цифре"
        assert card._emoji.text(), "пропал значок счётчика"
        assert card._value.text().isdigit(), (
            f"пропала цифра счётчика: {card._value.text()!r}"
        )


def test_stat_cards_never_narrower_than_content(window, log_view, monkeypatch):
    """Карточка не ужимается ниже «смайлик + цифра» — иначе цифра обрезалась бы.

    Место под карточки урезаем искусственно: в живом окне строка карточек ужимается
    не сильнее, чем по 40 px на карточку (значок + цифра + отступы), и предел виден
    только на этом запасе. Ширину содержимого считаем здесь же.

    Обновляем раскладку без processEvents: LogView.resizeEvent пересчитал бы ужим
    обратно по настоящей ширине окна.
    """
    monkeypatch.setattr(log_view, "_available_width", lambda: 200)
    log_view._apply_layout_mode(force=True)
    log_view.layout().activate()

    for card in stat_cards(log_view):
        content = (card._text_px(card._emoji.text(), 16) + card._GAP_MIN
                   + card._text_px(card._value.text(), 22) + 2 * card._PAD_MIN)
        assert card.width() >= card.icon_width() - 1, (
            f"карточка {card._label_text!r} ужата ниже значка: "
            f"{card.width()} < {card.icon_width()}"
        )
        assert card.width() >= content - 1, (
            f"карточка {card._label_text!r} уже содержимого: {card.width()} < {content}"
        )

    monkeypatch.undo()
    log_view._apply_layout_mode(force=True)
    settle(window)


def test_stat_cards_fit_available_width(window, log_view):
    """Сумма ширин карточек не выходит за место вкладки ни на одной ширине окна.

    Ширины считаются для каждой карточки отдельно и округляются: без общей сверки
    шесть округлений давали в сумме на 1–2 px больше места, и правый край
    последней карточки уходил за границу вкладки.
    """
    problems = []
    for width in range(560, 901, 20):
        set_width(window, width, panel_expanded=False)
        log_view._apply_layout_mode(force=True)
        settle(window)
        avail = log_view._available_width()
        cards = stat_cards(log_view)
        total = sum(c.width() for c in cards) + log_view._stat_gap * (len(cards) - 1)
        if total > avail:
            problems.append(f"окно={width}: карточки занимают {total} при {avail} px")
    assert not problems, "карточки вылезают за место вкладки: " + "; ".join(problems)


def test_stat_cards_shrink_evenly_not_one(window, log_view):
    """Лишние пиксели снимаются с самых широких карточек, а не с одной.

    Иначе одна карточка («Починка» — у неё самая широкая цифра) ужималась бы
    сильнее соседей и первой теряла подпись.
    """
    set_width(window, 580, panel_expanded=False)
    log_view._apply_layout_mode(force=True)
    settle(window)
    cards = stat_cards(log_view)
    widths = [c.width() for c in cards]
    natural = [c.icon_width() for c in cards]

    for card, width, floor in zip(cards, widths, natural, strict=True):
        assert width >= floor - 1, f"карточка {card._label_text!r} ужата ниже значка"
    assert max(widths) - min(widths) <= 60, (
        f"ширины карточек разошлись слишком сильно: {widths}"
    )


def test_stat_card_label_is_elided_not_cut(window, log_view):
    """Пока подпись видна, она укорачивается многоточием, а не режется краем."""
    set_width(window, 640, panel_expanded=False)
    log_view._apply_layout_mode(force=True)
    settle(window)

    checked = elided = 0
    for card in stat_cards(log_view):
        if not card._label.isVisible():
            continue
        text, full = card._label.text(), card._label_text
        assert text == full or (text.endswith("…") and full.startswith(text[:-1])), (
            f"подпись «{text}» не является началом «{full}» с многоточием"
        )
        checked += 1
        elided += text != full
    assert checked, "ни одной видимой подписи — тест ничего не проверил"
    assert elided, "ни одна подпись не укоротилась — тест не проверил обрезку"


def test_stat_card_tooltip_has_full_name(window, log_view):
    """Полное название счётчика всегда доступно в подсказке."""
    set_width(window, 580, panel_expanded=True)
    settle(window)
    tips = [c.toolTip() for c in stat_cards(log_view)]
    for expected in ("Всего", "Обход", "Напрямую", "Блок", "Починка", "Ошибка"):
        assert any(expected in tip for tip in tips), f"нет подсказки для «{expected}»"


def test_filter_chips_compress_to_emoji(window, log_view):
    """Кнопки-фильтры ужимаются до значка — тем же приёмом, что кнопки режимов.

    Полное состояние задаём напрямую: экран в песочнице 800 px, и даже на самом
    широком окне строка фильтров уже ужимается (она требует ≈1200 px).
    """
    chip = log_view._chips["routed"]
    chip.set_compression(0.0, force=True)
    settle(window)
    natural = chip.full_width()          # ширина подписи, как её посчитал Qt
    assert "Обход" in chip.text(), f"полное состояние: {chip.text()!r}"

    set_width(window, 580, panel_expanded=True)
    log_view._apply_layout_mode(force=True)
    settle(window)

    assert chip.width() < natural, f"фильтр не ужался: {natural} → {chip.width()}"
    assert chip.text() == "🚀", f"в узком окне остаётся значок, получили {chip.text()!r}"
    assert chip.toolTip() == "Обход", "название фильтра должно быть в подсказке"


def test_filter_chip_state_is_reversible(window, log_view):
    """Ужим обратим: вернули место — подписи фильтров вернулись."""
    chip = log_view._chips["direct"]
    chip.set_compression(0.0, force=True)
    settle(window)
    full_text = chip.text()
    full_width = chip.full_width()

    chip.set_compression(1.0, force=True)
    settle(window)
    assert chip.text() == "➡️", f"в ужатом виде ожидали значок, получили {chip.text()!r}"
    assert chip.width() < full_width, f"ширина не ужалась: {chip.width()} при подписи {full_width}"

    chip.set_compression(0.0, force=True)
    settle(window)
    assert chip.text() == full_text, f"подпись не вернулась: {chip.text()!r}"
    assert chip.full_width() == full_width, "естественная ширина не вернулась"


def test_search_icon_stays_visible_when_window_narrows(window, log_view):
    """Значок лупы в поле поиска не исчезает ни на одной ширине окна.

    Жалоба пользователя: «когда уменьшаешь страницу по горизонтали, смайлик лупы
    из поисковой строки исчезает». Поле поиска отдаёт место первым (фильтры
    останавливаются раньше — на ширине значка), и у него был только «свой» минимум
    Qt: он меньше, чем нужно значку вместе с отступами QSS, поэтому Qt заменял
    подсказку «🔍» многоточием. Проверяем, что место под текст всегда не меньше
    ширины значка.
    """
    field = log_view._search_input
    metrics = QFontMetrics(field.font())
    icon = max(log_view._SEARCH_ICON_MIN_PX,
               metrics.horizontalAdvance(log_view._SEARCH_ICON))

    problems = []
    for panel in (False, True):
        for width in range(560, 901, 20):
            set_width(window, width, panel_expanded=panel)
            room = field.width() - 2 * log_view._SEARCH_PAD - 2
            shown = metrics.elidedText(field.placeholderText(), Qt.ElideRight, room)
            if room < icon:
                problems.append(f"окно={width} панель={'да' if panel else 'нет'}: "
                                f"под текст {room} px, значку нужно {icon}")
            elif not shown.startswith(log_view._SEARCH_ICON):
                problems.append(f"окно={width} панель={'да' if panel else 'нет'}: "
                                f"покажет {shown!r}")
    assert not problems, "значок лупы пропадает: " + "; ".join(problems)


def test_search_field_keeps_icon_room_even_when_row_is_overfull(window, log_view, monkeypatch):
    """Даже когда строке физически не хватает места, поле не отдаёт место значка.

    Экран в песочнице 800 px: в живом окне фильтры ужимаются не до предела, и
    худший случай (строка шире вкладки) виден только на урезанном месте.
    """
    field = log_view._search_input
    monkeypatch.setattr(log_view, "_available_width", lambda: 120)
    log_view._apply_layout_mode(force=True)
    log_view.layout().activate()

    assert field.width() >= log_view._search_min_w, (
        f"поле поиска ужато ниже своего минимума: {field.width()} < {log_view._search_min_w}"
    )
    room = field.width() - 2 * log_view._SEARCH_PAD - 2
    assert room >= log_view._SEARCH_ICON_MIN_PX, f"значку лупы осталось {room} px"

    monkeypatch.undo()
    log_view._apply_layout_mode(force=True)
    settle(window)


def test_search_minimum_is_measured_not_hardcoded(window, log_view):
    """Минимум поля считается по значку и отступам, а не зашит числом.

    На другом шрифте (и на Windows со своим эмодзи-шрифтом) лупа шире —
    минимум должен ехать за ней, иначе значок снова начнёт пропадать.
    """
    field = log_view._search_input
    floor = log_view._search_min_width()
    assert floor == field.minimumWidth(), (
        f"у поля стоит минимум {field.minimumWidth()}, а расчёт даёт {floor} — "
        f"«съедаемый» минимум вернулся"
    )
    assert floor >= log_view._SEARCH_ICON_MIN_PX + 2 * log_view._SEARCH_PAD + 2, (
        "минимум не покрывает значок и отступы"
    )

    # 1) нижняя граница ширины значка (для шрифтов, где метрика эмодзи занижена)
    original_min = log_view._SEARCH_ICON_MIN_PX
    try:
        log_view._SEARCH_ICON_MIN_PX = 30
        assert log_view._search_min_width() == 30 + 2 * log_view._SEARCH_PAD + 2 \
            + log_view._SEARCH_SLACK, "нижняя граница ширины значка не учитывается"
    finally:
        log_view._SEARCH_ICON_MIN_PX = original_min

    # 2) сам значок: широкий значок = шире минимум (значит, меряется, а не зашит)
    original_icon = log_view._SEARCH_ICON
    try:
        log_view._SEARCH_ICON = "🔍🔍🔍🔍🔍🔍"
        assert log_view._search_min_width() > floor, "ширина значка не учитывается"
    finally:
        log_view._SEARCH_ICON = original_icon
    assert log_view._search_min_width() == floor, "минимум не вернулся после возврата значка"


def test_search_field_switches_to_icon_but_keeps_tooltip(window, log_view):
    """В ужатом виде подсказка поля — только значок, а название живёт в тултипе."""
    field = log_view._search_input
    set_width(window, 620, panel_expanded=False)
    log_view._apply_layout_mode(force=True)
    settle(window)
    assert field.placeholderText() == log_view._SEARCH_ICON, (
        f"ужатый вид поля: {field.placeholderText()!r}"
    )
    assert field.toolTip() == "Поиск по домену", "пропала подсказка у ужатого поля"


def test_pause_button_keeps_compression_after_click(window, log_view):
    """«Пауза» → «Продолжить» в ужатом виде: кнопка остаётся плотной и целой."""
    set_width(window, 580, panel_expanded=True)
    log_view._apply_layout_mode(force=True)
    settle(window)
    assert log_view._btn_pause.text() == "⏸", (
        f"подготовка теста: «Пауза» должна быть ужата, получили {log_view._btn_pause.text()!r}"
    )

    log_view._toggle_pause()
    settle(window)

    assert log_view._btn_pause.text() == "▶", (
        f"после клика «Пауза» перестала быть ужатой: {log_view._btn_pause.text()!r}"
    )
    assert log_view._btn_pause.toolTip() == "Продолжить", "пропала подсказка новой подписи"
    log_view._toggle_pause()          # возвращаем состояние
    settle(window)


def test_wide_log_view_shows_full_labels(window, log_view):
    """Широкое окно — прежний вид: подписи у карточек и фильтров на месте."""
    set_width(window, 820, panel_expanded=False)
    log_view._apply_layout_mode(force=True)
    settle(window)

    assert all(c._label.isVisible() for c in stat_cards(log_view)), "карточки потеряли подписи"

    for key, text in (("all", "🌐  Все"), ("routed", "🚀  Обход"), ("blocked", "⛔  Блок")):
        chip = log_view._chips[key]
        chip.set_compression(0.0, force=True)
        settle(window)
        assert chip.text() == text, f"полный вид фильтра: {chip.text()!r} вместо {text!r}"
