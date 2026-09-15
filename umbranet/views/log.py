"""
UmbraNet - раздел «Журнал» (PySide6) — телеграмизирован.

Живой поток DNS-запросов:
  • строка статистики сверху (всего / обход / напрямую / заблокировано);
  • поиск + фильтры (Все/Обход/Напрямую) + Пауза/Очистить + индикатор LIVE;
  • список строк с цветными бейджами (тип, маршрут, источник) и латентностью;
  • пустое состояние.

ТЕЛЕГРАМИЗАЦИЯ (фикс дёрганья при ресайзе):
  Раньше каждая строка была QFrame с 7 QLabel (200 строк = ~1400 виджетов).
  При ресайзе Qt делал layout.activate() на все 1400 — 50+ мс на ноуте.
  Теперь весь список рисует ОДИН paintEvent (LogCanvas, RowCanvas):
  только видимые ~15 строк за 2-3 мс, кэш, затухающий ползунок.

Архитектура обновлений:
  - Новые записи приходят через QueryLog.subscribe() из DNS-потока.
  - Прокидываются в UI через Qt-сигнал _entry_arrived (thread-safe).
  - refresh() при переключении вкладки НЕ перестраивает DOM —
    только подгружает записи которые пришли пока вкладка была скрыта.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QMenu,
)

from umbranet import theme
from umbranet.widgets.rounded_panel import RoundedPanel
from umbranet.engine_adapter import get_query_log
from umbranet.widgets.log_canvas import LogCanvas

SOURCE_LABELS = {
    "cache": "кэш", "stale-cache": "stale", "routed": "обход",
    "system": "система", "bogus-NX": "bogus", "blocked": "блок", "servfail": "ошибка",
    "bg-refresh": "фон", "fixed": "починка", "error": "ошибка",
    "check": "проверка", "leak": "утечка",
}

MAX_VISIBLE = 200   # лимит отображения (для canvas — виртуальный скролл, но лимит буфера)
MAX_BUFFER  = 2000  # строк в памяти


def _reason_for(entry) -> str:
    source = getattr(entry, "source", "")
    routed = getattr(entry, "routed", False)
    rcode = getattr(entry, "rcode", "")
    if source == "cache":
        return "свежий кэш"
    if source == "stale-cache":
        return "stale + фон"
    if source == "routed":
        return "домен в обходе" if routed else "secure"
    if source == "system":
        return "fallback/system"
    if source == "blocked":
        return "блоклист"
    if source == "bogus-NX":
        return "bogus-IP"
    if source == "servfail":
        return "не ответил"
    if source == "check":
        return "диагностика"
    if source == "leak":
        return "утечка"
    return rcode or "—"


class LogView(QWidget):
    _entry_arrived = Signal(object)

    def __init__(self):
        super().__init__()
        self.qlog = get_query_log()
        self._filter  = "all"
        self._search  = ""
        self._paused  = False
        self._rows: list = []

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
        self._stat_total   = self._stat_card("Всего",    "0", theme.ACCENT2)
        self._stat_routed  = self._stat_card("Обход",   "0", theme.ACCENT)
        self._stat_direct  = self._stat_card("Напрямую", "0", theme.SUBTEXT)
        self._stat_blocked = self._stat_card("Блок",    "0", theme.RED)
        self._stat_fixed   = self._stat_card("Починка", "0", theme.GREEN)
        self._stat_error   = self._stat_card("Ошибка",  "0", theme.ORANGE)
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
            if text == "мс":
                lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            elif text in ("Тип", "Маршрут", "Источник"):
                lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(f"color:{theme.MUTED};font-size:11px;font-weight:600;background:transparent;")
            chl.addWidget(lbl, stretch)
        outer.addWidget(colhead)

        # ── ТЕЛЕГРАМИЗИРОВАННЫЙ список (один canvas вместо 1400 виджетов) ──
        # Canvas сам рисует пустое состояние, поэтому отдельный QLabel не нужен
        self._canvas = LogCanvas()
        self._canvas.contextMenuRequested.connect(self._show_canvas_menu)
        self._canvas.domainCopied.connect(lambda d: QGuiApplication.clipboard().setText(d))
        outer.addWidget(self._canvas, 1)

        # ── подписка на новые записи из ядра ──
        self._entry_arrived.connect(self._add_row_ui)
        try:
            self.qlog.subscribe(self._on_entry_from_core)
        except Exception:
            pass

        self._last_entry_ts: float = 0.0
        self._live_blink_state: bool = False
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(600)
        self._live_timer.timeout.connect(self._tick_live)
        self._live_timer.start()

        self._set_filter("all")
        self.refresh()

    # ── вспомогательные ──
    def _stat_card(self, label: str, value: str, color: str) -> QWidget:
        f = RoundedPanel(theme.CARD, theme.BORDER, radius=12)
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
        if self._paused:
            return
        import time as _time
        now = _time.monotonic()
        active = (self._last_entry_ts > 0) and (now - self._last_entry_ts < 3.0)
        if active:
            self._live_blink_state = not self._live_blink_state
            color = theme.GREEN if self._live_blink_state else "#1a6632"
            self._live.setText("●  LIVE")
            self._live.setStyleSheet(f"color:{color};font-size:12px;font-weight:700;")
        else:
            self._live.setText("○  нет данных")
            self._live.setStyleSheet(f"color:{theme.MUTED};font-size:12px;font-weight:600;")

    # ── приём записей из ядра ──
    def _on_entry_from_core(self, entry):
        self._entry_arrived.emit(entry)

    def _add_row_ui(self, entry):
        import time as _time
        self._last_entry_ts = _time.monotonic()
        if self._paused:
            self._rows.append(entry)
            if len(self._rows) > MAX_BUFFER:
                self._rows = self._rows[-MAX_BUFFER:]
            return
        self._rows.append(entry)
        if len(self._rows) > MAX_BUFFER:
            self._rows = self._rows[-MAX_BUFFER:]
        # если проходит фильтр — обновляем canvas (виртуально, без перестроения всех виджетов)
        if self._passes_filter(entry):
            # canvas рисует только видимые, поэтому просто пересобираем отфильтрованный список
            # оптимизация: инкрементально добавлять, но проще пересобрать (быстро)
            self._update_canvas()
        self._update_stats()

    # ── фильтры ──
    def _passes_filter(self, entry) -> bool:
        if getattr(entry, "source", "") == "bg-refresh":
            return False
        if self._filter == "routed" and not getattr(entry, "routed", False):
            return False
        source = getattr(entry, "source", "")
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

    # ── пауза ──
    def _toggle_pause(self):
        self._paused = not self._paused
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
            self._sync_from_core()
            self._rebuild()

    # ── очистка ──
    def _clear(self):
        try:
            self.qlog.clear()
        except Exception:
            pass
        self._rows.clear()
        self._update_canvas()
        self._update_stats()

    # ── canvas ──
    def _filtered_entries(self):
        # новейшие сверху — как раньше reversed
        shown = [e for e in reversed(self._rows) if self._passes_filter(e)][:MAX_VISIBLE]
        return shown

    def _update_canvas(self):
        shown = self._filtered_entries()
        self._canvas.set_entries(shown)

    def _rebuild(self):
        self._update_canvas()
        self._update_stats()

    def _update_stats(self):
        visible = [e for e in self._rows if getattr(e, "source", "") != "bg-refresh"]
        total   = len(visible)
        routed  = sum(1 for e in visible if getattr(e, "routed", False))
        blocked = sum(1 for e in visible if getattr(e, "source", "") in ("bogus-NX", "blocked"))
        fixed   = sum(1 for e in visible if getattr(e, "source", "") == "fixed")
        errors  = sum(1 for e in visible if getattr(e, "source", "") in ("error", "servfail", "leak"))
        direct  = max(0, total - routed - fixed - errors - blocked)
        self._stat_total._value.setText(str(total))
        self._stat_routed._value.setText(str(routed))
        self._stat_direct._value.setText(str(direct))
        self._stat_blocked._value.setText(str(blocked))
        self._stat_fixed._value.setText(str(fixed))
        self._stat_error._value.setText(str(errors))

    # ── контекстное меню для canvas ──
    def _show_canvas_menu(self, idx: int, global_pos):
        entries = self._canvas.entries()
        if not (0 <= idx < len(entries)):
            return
        entry = entries[idx]
        domain = getattr(entry, "domain", "") or ""
        if not domain:
            return
        from umbranet.engine_adapter import get_engine, is_domain_allowed, is_domain_blocked, is_domain_routed
        import webbrowser
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background-color: {theme.CARD}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 4px; }}"
            f"QMenu::item {{ padding: 6px 20px 6px 12px; border-radius: 4px; font-size: 12px; }}"
            f"QMenu::item:selected {{ background-color: {theme.ACCENT}; color: {theme.WHITE}; }}"
            f"QMenu::item:disabled {{ color: {theme.MUTED}; }}"
        )
        eng = get_engine()
        cfg = eng.config
        is_routed = is_domain_routed(domain, cfg)
        is_blocked = is_domain_blocked(domain, cfg)
        is_allowed = is_domain_allowed(domain, cfg)

        act_route = QAction("➕ Добавить в обход", self)
        if is_routed:
            act_route.setText("✓ Уже в обходе")
            act_route.setEnabled(False)
        else:
            act_route.triggered.connect(lambda: self._add_to_bypass(domain))
        act_block = QAction("⛔ Заблокировать домен", self)
        if is_blocked:
            act_block.setText("✅ Убрать из блоклиста")
            act_block.triggered.connect(lambda: self._unblock_domain(domain))
        else:
            act_block.triggered.connect(lambda: self._block_domain(domain))
        act_allow = QAction("🟢 Добавить в allowlist", self)
        if is_allowed:
            act_allow.setText("✅ Убрать из allowlist")
            act_allow.triggered.connect(lambda: self._unallow_domain(domain))
        else:
            act_allow.triggered.connect(lambda: self._allow_domain(domain))
        act_copy = QAction("📋 Скопировать домен", self)
        act_copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(domain))
        act_copy_row = QAction("📋 Скопировать строку", self)
        act_copy_row.triggered.connect(lambda: self._copy_row(entry))
        act_open = QAction("🌐 Открыть в браузере", self)
        act_open.triggered.connect(lambda: webbrowser.open(f"https://{domain}"))

        menu.addAction(act_route)
        menu.addAction(act_block)
        menu.addAction(act_allow)
        menu.addSeparator()
        menu.addAction(act_copy)
        menu.addAction(act_copy_row)
        menu.addAction(act_open)
        menu.exec(global_pos)

    def _add_to_bypass(self, domain):
        from umbranet.engine_adapter import get_engine, save_config
        eng = get_engine()
        cfg = eng.config
        routed = cfg.setdefault("routed_domains", [])
        if domain and domain not in routed:
            routed.append(domain)
            save_config(cfg)
            eng.reload_config()
            self._rebuild()

    def _block_domain(self, domain):
        from umbranet.engine_adapter import block_domain
        if block_domain(domain):
            self._rebuild()

    def _unblock_domain(self, domain):
        from umbranet.engine_adapter import unblock_domain
        if unblock_domain(domain):
            self._rebuild()

    def _allow_domain(self, domain):
        from umbranet.engine_adapter import allow_domain
        if allow_domain(domain):
            self._rebuild()

    def _unallow_domain(self, domain):
        from umbranet.engine_adapter import unallow_domain
        if unallow_domain(domain):
            self._rebuild()

    def _copy_row(self, entry):
        ts = time.strftime("%H:%M:%S", time.localtime(getattr(entry, "timestamp", 0)))
        text = (
            f"{ts}\t{getattr(entry, 'domain', '')}\t{getattr(entry, 'qtype', '')}\t"
            f"{SOURCE_LABELS.get(getattr(entry, 'source', ''), getattr(entry, 'source', ''))}\t"
            f"{getattr(entry, 'rcode', '')}\t{getattr(entry, 'latency_ms', '')} мс\t"
            f"{getattr(entry, 'note', '')}"
        )
        QGuiApplication.clipboard().setText(text)

    # ── синхронизация с ядром ──
    def _sync_from_core(self):
        try:
            snap = self.qlog.snapshot()
        except Exception:
            return
        if not snap:
            return
        last_ts = getattr(self._rows[-1], "timestamp", 0) if self._rows else 0
        new = [e for e in snap if getattr(e, "timestamp", 0) > last_ts]
        for e in new:
            self._rows.append(e)
        if len(self._rows) > MAX_BUFFER:
            self._rows = self._rows[-MAX_BUFFER:]

    def _export_clipboard(self):
        lines = ["Время\tДомен\tТип\tМаршрут\tИсточник\tПричина\tмс"]
        for e in self._rows:
            if not self._passes_filter(e):
                continue
            ts = time.strftime("%H:%M:%S", time.localtime(getattr(e, "timestamp", 0)))
            domain = getattr(e, "domain", "")
            qtype = getattr(e, "qtype", "")
            routed = "обход" if getattr(e, "routed", False) else "напрямую"
            source = SOURCE_LABELS.get(getattr(e, "source", ""), getattr(e, "source", ""))
            reason = getattr(e, "note", "") or _reason_for(e)
            latency = str(getattr(e, "latency_ms", 0) or "")
            lines.append(f"{ts}\t{domain}\t{qtype}\t{routed}\t{source}\t{reason}\t{latency}")
        QGuiApplication.clipboard().setText("\n".join(lines))

    def refresh(self):
        self._sync_from_core()
        # если ещё пусто — rebuild покажет empty, иначе обновит canvas/статы
        if not self._rows:
            self._rebuild()
        else:
            self._update_stats()
            # canvas уже обновляется через _add_row_ui / _rebuild, но на вход на вкладку
            # надо убедиться что фильтр применён
            self._update_canvas()
