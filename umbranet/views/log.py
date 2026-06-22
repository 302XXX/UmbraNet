"""
UmbraNet - раздел «Журнал» (PySide6).

Живой поток DNS-запросов:
  • строка статистики сверху (всего / обход / напрямую / заблокировано);
  • поиск + фильтры (Все/Обход/Напрямую) + Пауза/Очистить + индикатор LIVE;
  • список строк с цветными бейджами (тип, маршрут, источник) и латентностью;
  • пустое состояние.

Архитектура обновлений:
  - Новые записи приходят через QueryLog.subscribe() из DNS-потока.
  - Прокидываются в UI через Qt-сигнал _entry_arrived (thread-safe).
  - refresh() при переключении вкладки НЕ перестраивает список заново —
    только подгружает записи которые пришли пока вкладка была скрыта.
  - Таким образом список "живёт" непрерывно без flash при переключении.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import get_query_log

SOURCE_LABELS = {
    "cache": "кэш", "stale-cache": "stale", "routed": "обход",
    "system": "система", "bogus-NX": "bogus", "blocked": "блок", "servfail": "ошибка",
    "bg-refresh": "фон", "fixed": "починка", "error": "ошибка",
    "check": "проверка", "leak": "утечка",
}
SOURCE_COLORS = {
    "routed": theme.ACCENT, "system": theme.SUBTEXT, "cache": theme.ACCENT3,
    "stale-cache": theme.ACCENT3, "bogus-NX": theme.RED, "blocked": theme.RED, "servfail": theme.RED,
    "fixed": theme.GREEN, "error": theme.ORANGE,
    "check": theme.ACCENT2, "leak": theme.RED,
}

MAX_VISIBLE = 200   # строк в DOM
MAX_BUFFER  = 2000  # строк в памяти


def _badge(text: str, color: str, filled: bool = False, width: int = 0) -> QLabel:
    b = QLabel(text)
    b.setAlignment(Qt.AlignCenter)
    # Если задаем фикс ширину, убираем padding по бокам, чтобы выравнивание работало ровно
    pad = "0" if width else "9px"
    if filled:
        b.setStyleSheet(
            f"background:{color};color:{theme.WHITE};border-radius:8px;"
            f"padding:2px {pad};font-size:11px;font-weight:600;")
    else:
        b.setStyleSheet(
            f"background:transparent;color:{color};border:1px solid {color};"
            f"border-radius:8px;padding:2px {pad};font-size:11px;font-weight:600;")
    if width:
        b.setFixedWidth(width)
    return b


class _LogRow(QFrame):
    """Одна строка журнала."""
    def __init__(self, entry, index: int, parent_view=None):
        super().__init__()
        self._parent_view = parent_view
        self._entry = entry
        bg = theme.CARD if index % 2 == 0 else theme.INPUT_BG
        self.setStyleSheet(
            f"_LogRow{{background:{bg};border-radius:8px;}}"
            f"_LogRow:hover{{background:{theme.CARD_TOP};}}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(10)

        ts     = time.strftime("%H:%M:%S", time.localtime(getattr(entry, "timestamp", time.time())))
        domain = getattr(entry, "domain", "")
        self.domain = domain
        qtype  = getattr(entry, "qtype", "")
        routed = getattr(entry, "routed", False)
        source = getattr(entry, "source", "")
        latency = getattr(entry, "latency_ms", 0)
        note = getattr(entry, "note", "") or self._reason_for(entry)

        t = QLabel(ts)
        t.setFixedWidth(64)
        t.setStyleSheet(f"color:{theme.MUTED};font-size:12px;font-family:Consolas;background:transparent;")
        lay.addWidget(t)

        d = QLabel(domain)
        d.setMinimumWidth(30)
        from PySide6.QtWidgets import QSizePolicy
        d.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        d.setStyleSheet(f"color:{theme.TEXT};font-size:13px;font-family:Consolas;background:transparent;")
        lay.addWidget(d, 1)

        lay.addWidget(_badge(qtype or "?", theme.ACCENT2, width=54))

        if routed:
            lay.addWidget(_badge("обход", theme.ACCENT, filled=True, width=64))
        else:
            lay.addWidget(_badge("напрямую", theme.MUTED, width=64))

        sc = SOURCE_COLORS.get(source, theme.SUBTEXT)
        lay.addWidget(_badge(SOURCE_LABELS.get(source, source or "—"), sc, width=64))

        reason = QLabel(note or self._reason_for(entry))
        reason.setFixedWidth(140)
        # Убираем Ignored, чтобы reason всегда был ровно 140 пикселей (обрезка через elide если надо)
        reason.setToolTip(note or self._reason_for(entry))
        reason.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;")
        lay.addWidget(reason, stretch=0)

        if latency:
            lcol = theme.GREEN if latency < 50 else (theme.YELLOW if latency < 150 else theme.RED)
            lat = QLabel(f"{latency} мс")
        else:
            lcol = theme.MUTED
            lat = QLabel("—")
        lat.setFixedWidth(56)
        lat.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lat.setStyleSheet(f"color:{lcol};font-size:12px;font-family:Consolas;background:transparent;")
        lay.addWidget(lat)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    @staticmethod
    def _reason_for(entry) -> str:
        source = getattr(entry, "source", "")
        routed = getattr(entry, "routed", False)
        rcode = getattr(entry, "rcode", "")
        if source == "cache":
            return "свежий кэш"
        if source == "stale-cache":
            return "stale + фон. обновление"
        if source == "routed":
            return "домен в обходе" if routed else "secure upstream"
        if source == "system":
            return "fallback/system DNS"
        if source == "blocked":
            return "ручной блоклист"
        if source == "bogus-NX":
            return "bogus-IP провайдера"
        if source == "servfail":
            return "upstream не ответил"
        if source == "check":
            return "диагностика"
        if source == "leak":
            return "DNS/IPv6 утечка"
        return rcode or "—"

    def _show_context_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction
        import webbrowser
        from umbranet.engine_adapter import (
            get_engine, save_config, is_domain_routed, is_domain_blocked, is_domain_allowed,
            block_domain, unblock_domain, allow_domain, unallow_domain,
        )

        if not self.domain:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: {theme.CARD}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 20px 6px 12px; border-radius: 4px; font-size: 12px; }}"
            f"QMenu::item:selected {{ background-color: {theme.ACCENT}; color: {theme.WHITE}; }}"
            f"QMenu::item:disabled {{ color: {theme.MUTED}; }}"
        )

        eng = get_engine()
        cfg = eng.config
        is_routed = is_domain_routed(self.domain, cfg)
        is_blocked = is_domain_blocked(self.domain, cfg)
        is_allowed = is_domain_allowed(self.domain, cfg)

        act_route = QAction("➕ Добавить в обход", self)
        if is_routed:
            act_route.setText("✓ Уже в обходе")
            act_route.setEnabled(False)
        else:
            act_route.triggered.connect(self._add_to_bypass)

        act_block = QAction("⛔ Заблокировать домен", self)
        if is_blocked:
            act_block.setText("✅ Убрать из блоклиста")
            act_block.triggered.connect(lambda: self._unblock_domain())
        else:
            act_block.triggered.connect(lambda: self._block_domain())

        act_allow = QAction("🟢 Добавить в allowlist", self)
        if is_allowed:
            act_allow.setText("✅ Убрать из allowlist")
            act_allow.triggered.connect(lambda: self._unallow_domain())
        else:
            act_allow.triggered.connect(lambda: self._allow_domain())

        act_copy = QAction("📋 Скопировать домен", self)
        act_copy.triggered.connect(self._copy_domain)

        act_copy_row = QAction("📋 Скопировать строку", self)
        act_copy_row.triggered.connect(self._copy_row)

        act_open = QAction("🌐 Открыть в браузере", self)
        act_open.triggered.connect(lambda: webbrowser.open(f"https://{self.domain}"))

        menu.addAction(act_route)
        menu.addAction(act_block)
        menu.addAction(act_allow)
        menu.addSeparator()
        menu.addAction(act_copy)
        menu.addAction(act_copy_row)
        menu.addAction(act_open)

        menu.exec(self.mapToGlobal(pos))

    def _add_to_bypass(self):
        from umbranet.engine_adapter import get_engine, save_config
        eng = get_engine()
        cfg = eng.config
        routed = cfg.setdefault("routed_domains", [])
        if self.domain and self.domain not in routed:
            routed.append(self.domain)
            save_config(cfg)
            eng.reload_config()
            if self._parent_view:
                self._parent_view._rebuild()

    def _block_domain(self):
        from umbranet.engine_adapter import block_domain
        if block_domain(self.domain) and self._parent_view:
            self._parent_view._rebuild()

    def _unblock_domain(self):
        from umbranet.engine_adapter import unblock_domain
        if unblock_domain(self.domain) and self._parent_view:
            self._parent_view._rebuild()

    def _allow_domain(self):
        from umbranet.engine_adapter import allow_domain
        if allow_domain(self.domain) and self._parent_view:
            self._parent_view._rebuild()

    def _unallow_domain(self):
        from umbranet.engine_adapter import unallow_domain
        if unallow_domain(self.domain) and self._parent_view:
            self._parent_view._rebuild()

    def _copy_domain(self):
        from PySide6.QtGui import QGuiApplication
        if self.domain:
            QGuiApplication.clipboard().setText(self.domain)

    def _copy_row(self):
        from PySide6.QtGui import QGuiApplication
        e = self._entry
        ts = time.strftime("%H:%M:%S", time.localtime(getattr(e, "timestamp", 0)))
        text = (
            f"{ts}\t{getattr(e, 'domain', '')}\t{getattr(e, 'qtype', '')}\t"
            f"{SOURCE_LABELS.get(getattr(e, 'source', ''), getattr(e, 'source', ''))}\t"
            f"{getattr(e, 'rcode', '')}\t{getattr(e, 'latency_ms', '')} мс\t"
            f"{getattr(e, 'note', '')}"
        )
        QGuiApplication.clipboard().setText(text)


class LogView(QWidget):
    # Сигнал для безопасной передачи записей из DNS-потока в UI-поток
    _entry_arrived = Signal(object)

    def __init__(self):
        super().__init__()
        self.qlog = get_query_log()
        self._filter  = "all"
        self._search  = ""
        self._paused  = False
        self._rows: list = []       # все записи в памяти (буфер)
        self._pending: list = []    # записи пришедшие пока вкладка скрыта

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        # ── заголовок + LIVE ──
        head = QHBoxLayout()
        title = QLabel("Логи запросов")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:22px;font-weight:700;")
        head.addWidget(title)
        self._live = QLabel("●  LIVE")
        self._live.setStyleSheet(f"color:{theme.GREEN};font-size:12px;font-weight:700;")
        head.addSpacing(10)
        head.addWidget(self._live)
        head.addStretch()

        # Кнопка экспорта
        btn_export = QPushButton("📋 Копировать")
        btn_export.setCursor(Qt.PointingHandCursor)
        btn_export.setFixedHeight(32)
        btn_export.setStyleSheet(self._chip_qss(False))
        btn_export.clicked.connect(self._export_clipboard)
        head.addWidget(btn_export)
        outer.addLayout(head)

        # ── карточки статистики ──
        stats = QHBoxLayout()
        stats.setSpacing(12)
        self._stat_total   = self._stat_card("Всего",         "0", theme.ACCENT2)
        self._stat_routed  = self._stat_card("Обход",   "0", theme.ACCENT)
        self._stat_direct  = self._stat_card("Напрямую",      "0", theme.SUBTEXT)
        self._stat_blocked = self._stat_card("Блок", "0", theme.RED)
        self._stat_fixed   = self._stat_card("Починка",       "0", theme.GREEN)
        self._stat_error   = self._stat_card("Ошибка",        "0", theme.ORANGE)
        for c in (self._stat_total, self._stat_routed, self._stat_direct, self._stat_blocked, self._stat_fixed, self._stat_error):
            stats.addWidget(c, 1)
        outer.addLayout(stats)

        # ── панель управления ──
        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍  Поиск по домену...")
        self._search_input.setFixedHeight(36)
        self._search_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 12px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        self._search_input.textChanged.connect(self._on_search)
        ctrl.addWidget(self._search_input, 1)

        self._chips = {}
        for key, label in [
            ("all", "Все"), ("routed", "Обход"), ("direct", "Напрямую"),
            ("blocked", "Блок"), ("fixed", "Починки"), ("errors", "Ошибки"), ("cache", "Кэш"),
        ]:
            chip = QPushButton(label)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setFixedHeight(36)
            chip.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            self._chips[key] = chip
            ctrl.addWidget(chip)

        self._btn_pause = QPushButton("⏸  Пауза")
        self._btn_pause.setCursor(Qt.PointingHandCursor)
        self._btn_pause.setFixedHeight(36)
        self._btn_pause.setStyleSheet(self._chip_qss(False))
        self._btn_pause.clicked.connect(self._toggle_pause)
        ctrl.addWidget(self._btn_pause)

        btn_clear = QPushButton("🗑  Очистить")
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.setFixedHeight(36)
        btn_clear.setStyleSheet(self._chip_qss(False))
        btn_clear.clicked.connect(self._clear)
        ctrl.addWidget(btn_clear)
        outer.addLayout(ctrl)

        # ── шапка столбцов ──
        colhead = QFrame()
        colhead.setStyleSheet("background:transparent;")
        chl = QHBoxLayout(colhead)
        # Отступ справа больше на 14px, чтобы компенсировать ширину скроллбара
        chl.setContentsMargins(12, 0, 26, 0)
        chl.setSpacing(10)
        for text, w, stretch in [
            ("Время",   64, 0), ("Домен",    0,   1), ("Тип",    54, 0),
            ("Маршрут", 64, 0), ("Источник", 64,  0),
            ("Причина", 140, 0), ("мс",       56,  0),
        ]:
            lbl = QLabel(text)
            if w:
                lbl.setFixedWidth(w)
            else:
                from PySide6.QtWidgets import QSizePolicy
                lbl.setMinimumWidth(30)
                lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            # Для "мс" выравниваем вправо, чтобы совпадало с цифрами пинга
            if text == "мс":
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            elif text in ("Тип", "Маршрут", "Источник"):
                lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(f"color:{theme.MUTED};font-size:11px;font-weight:600;background:transparent;")
            chl.addWidget(lbl, stretch)
        outer.addWidget(colhead)

        # ── список строк ──
        self._list = QVBoxLayout()
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(3)
        self._list.setAlignment(Qt.AlignTop)
        listw = QWidget()
        listw.setLayout(self._list)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())
        self._scroll.setWidget(listw)
        outer.addWidget(self._scroll, 1)

        # пустое состояние
        self._empty = QLabel(
            "📭  Пока нет запросов\n\nЗапустите DNS — здесь появится живой поток.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(f"color:{theme.MUTED};font-size:15px;")
        self._list.addWidget(self._empty)

        # ── подписка на новые записи из ядра ──
        self._entry_arrived.connect(self._add_row_ui)
        try:
            self.qlog.subscribe(self._on_entry_from_core)
        except Exception:
            pass

        # ── таймер LIVE-индикатора ──
        # _last_entry_ts: время последней полученной записи (0 = ничего не было).
        # Таймер раз в секунду проверяет: если запись пришла менее 3 сек назад —
        # мигаем (данные идут), иначе — статичная серая точка (тихо).
        self._last_entry_ts: float = 0.0
        self._live_blink_state: bool = False
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(600)
        self._live_timer.timeout.connect(self._tick_live)
        self._live_timer.start()

        self._set_filter("all")
        self.refresh()

    # ── вспомогательные ──────────────────────────────────────────────────────

    def _stat_card(self, label: str, value: str, color: str) -> QFrame:
        f = QFrame()
        f.setStyleSheet(f"QFrame{{{theme.card_qss(12)}}}")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        v = QLabel(value)
        v.setStyleSheet(f"color:{color};font-size:22px;font-weight:700;background:transparent;border:none;")
        l = QLabel(label)
        l.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(v)
        lay.addWidget(l)
        f._value = v
        return f

    def _chip_qss(self, active: bool) -> str:
        if active:
            return (f"QPushButton{{background:{theme.brand_grad()};color:{theme.WHITE};"
                    "border:none;border-radius:10px;padding:0 16px;font-weight:600;}}")
        return (f"QPushButton{{background:{theme.CARD};color:{theme.SUBTEXT};"
                f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 16px;}}"
                f"QPushButton:hover{{color:{theme.TEXT};border-color:{theme.ACCENT};}}")

    def _tick_live(self):
        """Обновляет индикатор LIVE по реальному состоянию потока данных.

        Логика:
          - DNS не запущен или данных не было давно → серая статичная точка «○  нет данных»
          - Данные идут (запись пришла менее 3 сек назад) → зелёная мигающая «●  LIVE»
          - Пауза → жёлтая «⏸  ПАУЗА» (устанавливается в _toggle_pause, здесь не трогаем)
        """
        if self._paused:
            return

        import time as _time
        now = _time.monotonic()
        active = (self._last_entry_ts > 0) and (now - self._last_entry_ts < 3.0)

        if active:
            # Мигаем: чередуем яркую и тёмную точку
            self._live_blink_state = not self._live_blink_state
            color = theme.GREEN if self._live_blink_state else "#1a6632"
            self._live.setText("●  LIVE")
            self._live.setStyleSheet(f"color:{color};font-size:12px;font-weight:700;")
        else:
            # Тихо: нет данных — серая статичная точка
            self._live.setText("○  нет данных")
            self._live.setStyleSheet(f"color:{theme.MUTED};font-size:12px;font-weight:600;")

    # ── приём записей из ядра ─────────────────────────────────────────────────

    def _on_entry_from_core(self, entry):
        """Вызывается из DNS-потока — передаём в UI через сигнал."""
        self._entry_arrived.emit(entry)

    def _add_row_ui(self, entry):
        """Вызывается в UI-потоке — добавляем строку."""
        import time as _time
        self._last_entry_ts = _time.monotonic()   # фиксируем время последней записи

        if self._paused:
            # На паузе пишем в буфер, но не показываем
            self._rows.append(entry)
            if len(self._rows) > MAX_BUFFER:
                self._rows = self._rows[-MAX_BUFFER:]
            return

        self._rows.append(entry)
        if len(self._rows) > MAX_BUFFER:
            self._rows = self._rows[-MAX_BUFFER:]

        if self._passes_filter(entry):
            self._prepend_row(entry)

        self._update_stats()

    # ── фильтры ──────────────────────────────────────────────────────────────

    def _passes_filter(self, entry) -> bool:
        if getattr(entry, "source", "") == "bg-refresh":
            return False
        if self._filter == "routed" and not getattr(entry, "routed", False):
            return False
        source = getattr(entry, "source", "")
        # Если фильтр "Напрямую" - показываем только прямые DNS-запросы, 
        # исключая системные служебные логи (fixed/error)
        if self._filter == "direct" and (getattr(entry, "routed", False) or source in ("fixed", "error")):
            return False
        if self._filter == "blocked" and source not in ("blocked", "bogus-NX"):
            return False
        if self._filter == "fixed" and source != "fixed":
            return False
        if self._filter == "errors" and source not in ("servfail", "bogus-NX", "blocked", "error", "leak"):
            return False
        if self._filter == "cache" and source not in ("cache", "stale-cache"):
            return False
        if self._search:
            hay = " ".join([
                getattr(entry, "domain", ""), getattr(entry, "qtype", ""),
                getattr(entry, "source", ""), getattr(entry, "rcode", ""),
                getattr(entry, "note", ""), ",".join(getattr(entry, "answers", []) or []),
            ]).lower()
            if self._search not in hay:
                return False
        return True

    def _set_filter(self, key: str):
        self._filter = key
        for k, chip in self._chips.items():
            chip.setStyleSheet(self._chip_qss(k == key))
        self._rebuild()

    def _on_search(self, text: str):
        self._search = (text or "").lower().strip()
        self._rebuild()

    # ── пауза ─────────────────────────────────────────────────────────────────

    def _toggle_pause(self):
        self._paused = not self._paused
        # Синхронизируем пазу с ядром — при паузе ядро не уведомляет подписчиков,
        # но продолжает писать записи в ring-buffer.
        try:
            self.qlog.set_paused(self._paused)
        except Exception:
            pass

        if self._paused:
            self._btn_pause.setText("▶  Продолжить")
            self._live.setText("⏸  ПАУЗА")
            self._live.setStyleSheet(f"color:{theme.YELLOW};font-size:12px;font-weight:700;")
        else:
            self._btn_pause.setText("⏸  Пауза")
            self._live.setText("●  LIVE")
            self._live.setStyleSheet(f"color:{theme.GREEN};font-size:12px;font-weight:700;")
            # После снятия паузы подгружаем что накопилось в ring-buffer ядра
            self._sync_from_core()
            self._rebuild()

    # ── очистка ───────────────────────────────────────────────────────────────

    def _clear(self):
        try:
            self.qlog.clear()
        except Exception:
            pass
        self._rows.clear()
        self._clear_list()
        self._show_empty()
        self._update_stats()

    # ── наполнение DOM ────────────────────────────────────────────────────────

    def _clear_list(self):
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()

    def _show_empty(self):
        self._empty = QLabel(
            "📭  Пока нет запросов\n\nЗапустите DNS — здесь появится живой поток.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(f"color:{theme.MUTED};font-size:15px;")
        self._list.addWidget(self._empty)

    def _prepend_row(self, entry):
        """Добавляет строку в начало списка — без полного перестроения."""
        # Убираем пустое состояние если есть
        if self._empty is not None and self._empty.parent() is not None:
            self._empty.hide()
            self._empty.deleteLater()
            self._empty = None

        idx = self._list.count()
        self._list.insertWidget(0, _LogRow(entry, idx, self))

        # Убираем лишние строки снизу
        while self._list.count() > MAX_VISIBLE:
            item = self._list.takeAt(self._list.count() - 1)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()

    def _rebuild(self):
        """Полное перестроение списка — только при смене фильтра/поиска/очистке."""
        self._clear_list()
        self._empty = None

        shown = [e for e in reversed(self._rows) if self._passes_filter(e)][:MAX_VISIBLE]
        if not shown:
            self._show_empty()
        else:
            for i, e in enumerate(shown):
                self._list.addWidget(_LogRow(e, i, self))

        self._update_stats()

    def _update_stats(self):
        visible = [e for e in self._rows if getattr(e, "source", "") != "bg-refresh"]
        total   = len(visible)
        routed  = sum(1 for e in visible if getattr(e, "routed", False))
        blocked = sum(1 for e in visible if getattr(e, "source", "") in ("bogus-NX", "blocked"))
        fixed   = sum(1 for e in visible if getattr(e, "source", "") == "fixed")
        errors  = sum(1 for e in visible if getattr(e, "source", "") in ("error", "servfail", "leak"))
        direct  = total - routed - fixed - errors - blocked # Приблизительно
        if direct < 0: direct = 0
        self._stat_total._value.setText(str(total))
        self._stat_routed._value.setText(str(routed))
        self._stat_direct._value.setText(str(direct))
        self._stat_blocked._value.setText(str(blocked))
        self._stat_fixed._value.setText(str(fixed))
        self._stat_error._value.setText(str(errors))

    # ── синхронизация с ядром ─────────────────────────────────────────────────

    def _sync_from_core(self):
        """Подгружает из ring-buffer ядра записи которые пришли в буфер
        пока подписчик не уведомлялся (например на паузе).
        Добавляет только те записи которых нет в нашем _rows.
        """
        try:
            snap = self.qlog.snapshot()
        except Exception:
            return
        if not snap:
            return
        # Берём только новые записи по timestamp — те что старше последнего
        last_ts = getattr(self._rows[-1], "timestamp", 0) if self._rows else 0
        new = [e for e in snap if getattr(e, "timestamp", 0) > last_ts]
        for e in new:
            self._rows.append(e)
        if len(self._rows) > MAX_BUFFER:
            self._rows = self._rows[-MAX_BUFFER:]

    # ── экспорт ───────────────────────────────────────────────────────────────

    def _export_clipboard(self):
        """Копирует отфильтрованные записи в буфер обмена в виде TSV."""
        lines = ["Время\tДомен\tТип\tМаршрут\tИсточник\tПричина\tмс"]
        for e in self._rows:
            if not self._passes_filter(e):
                continue
            ts      = time.strftime("%H:%M:%S", time.localtime(getattr(e, "timestamp", 0)))
            domain  = getattr(e, "domain", "")
            qtype   = getattr(e, "qtype", "")
            routed  = "обход" if getattr(e, "routed", False) else "напрямую"
            source  = SOURCE_LABELS.get(getattr(e, "source", ""), getattr(e, "source", ""))
            reason = getattr(e, "note", "") or _LogRow._reason_for(e)
            latency = str(getattr(e, "latency_ms", 0) or "")
            lines.append(f"{ts}\t{domain}\t{qtype}\t{routed}\t{source}\t{reason}\t{latency}")
        QGuiApplication.clipboard().setText("\n".join(lines))

    # ── публичный refresh (вызывается при переключении на вкладку) ─────────────

    def refresh(self):
        """Вызывается при переходе на вкладку.

        НЕ перестраивает список заново — это вызывало flash.
        Только подгружает записи которые накопились пока вкладка была скрыта.
        """
        # Подгружаем новые записи из ядра (те что пришли пока подписчик молчал)
        self._sync_from_core()

        # Если список пустой — покажем пустое состояние или заполним
        if self._list.count() == 0:
            self._rebuild()
        else:
            # Обновляем только статистику — список уже актуален
            self._update_stats()
