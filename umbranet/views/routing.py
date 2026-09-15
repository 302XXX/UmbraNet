"""
UmbraNet - раздел «Маршрутизация» (PySide6).

Состав:
  • шапка: режимы (ModeSwitch) + Start/Stop/Restart (ControlBar);
  • категории сервисов с тумблерами (включил -> домены в routed_domains);
  • ручной список доменов/процессов (поиск, добавление, удаление);
  • правая панель активного DNS-профиля.

Вся работа с ядром — через engine_adapter.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QThread, Signal

log = logging.getLogger("UmbraNet.RoutingView")
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    get_active_dns_profile,
    get_current_mode,
    get_engine,
    get_favorite_services,
    is_domain_routed,
    save_config,
    set_favorite_services,
)
from umbranet.services_catalog import (
    CATEGORIES,
    SERVICES,
    services_in_category,
)
from umbranet.widgets.collapsible import Collapsible
from umbranet.widgets.dialogs import ProcessPickerDialog
from umbranet.widgets.glow_wrap import GlowWrap
from umbranet.widgets.rounded_panel import RoundedPanel
from umbranet.widgets.manual_canvas import ManualCanvas
from umbranet.widgets.service_canvas import ServiceCanvas
from umbranet.widgets.toggle import Toggle
from umbranet.widgets.transport_list import TransportList


class _DnsRestartWorker(QThread):
    """Мягко перезапускает DNS-сервер после изменения маршрутов.

    Системный DNS Windows при этом не трогаем: он уже указывает на 127.0.0.1.
    Нужен именно restart локального сервера, чтобы изменения маршрутизации
    применялись сразу и очищался кэш старых решений.
    """
    done = Signal(bool)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def run(self):
        ok = False
        try:
            self.engine.stop()
            self.engine.reload_config()
            # Если пользователь/AI-генерация успели нажать Stop, пока этот
            # worker выполнялся, нельзя самовольно запускать UmbraNet обратно.
            if getattr(self.engine, "_manual_stop_requested", False):
                ok = True
            else:
                ok = bool(self.engine.start())
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось перезапустить DNS после смены маршрута: %s", exc)
            ok = False
        self.done.emit(ok)


def hex_to_rgba(hex_str: str, alpha: float = 0.35) -> str:
    """Вспомогательный хелпер для конвертации HEX цветов в RGBA с прозрачностью."""
    hex_str = hex_str.lstrip("#")
    if len(hex_str) == 6:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        return f"rgba({r}, {g}, {b}, {alpha})"
    return f"rgba(255, 255, 255, {alpha})"


class CategoryHeader(QWidget):
    """Красивый заголовок категории с неоновой разделительной линией сверху и тумблером."""
    def __init__(self, title: str, emoji: str, color1: str, color2: str, toggle: Toggle | None = None, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;border:none;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 4)
        lay.setSpacing(6)

        # Тонкий разделитель сверху (яркий, длинный и выразительный, в цвете категории)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.grad(color1, 'rgba(255,255,255,0.03)')}; border: none;")
        lay.addWidget(line)

        # Строка с названием и тумблером (полностью прозрачная на фоне, без дешевого блюра/плашек)
        row = QHBoxLayout()
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(8)

        # Отдельный лейбл для эмодзи с увеличенной шириной до 32px и отступом справа, чтобы значки не обрезались
        icon_lbl = QLabel(emoji)
        icon_lbl.setStyleSheet("font-size: 15px; background: transparent; border: none; padding-right: 3px;")
        icon_lbl.setFixedWidth(32)
        icon_lbl.setAlignment(Qt.AlignCenter)
        row.addWidget(icon_lbl)

        # Название категории
        lbl = QLabel(title.upper())
        lbl.setStyleSheet(f"color: {color1}; font-size: 11px; font-weight: 800; letter-spacing: 1.5px; background: transparent; border: none;")
        row.addWidget(lbl)
        
        # Растяжка между названием и тумблером, чтобы тумблер оставался справа в ряду с другими
        row.addStretch()

        if toggle is not None:
            row.addWidget(toggle)
            
        # Небольшой отступ справа (10px), чтобы тумблеры категорий были гармонично выровнены
        row.addSpacing(10)

        lay.addLayout(row)


class CategorySection(QWidget):
    """Статичный контейнер категории услуг с красивой шапкой."""
    def __init__(self, title: str, emoji: str, color1: str, color2: str, toggle: Toggle | None = None, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:transparent;border:none;")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)

        # Добавляем красивую шапку-разделитель
        self.header = CategoryHeader(title, emoji, color1, color2, toggle)
        self.lay.addWidget(self.header)

        # Контейнер для строк сервисов
        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background:transparent;border:none;")
        self.content_lay = QVBoxLayout(self.content_widget)
        self.content_lay.setContentsMargins(0, 0, 0, 0)
        self.content_lay.setSpacing(4)
        self.lay.addWidget(self.content_widget)

        # Алиас для 100% обратной совместимости со старым кодом Collapsible
        self._body_lay = self.content_lay

    def add_widget(self, w: QWidget):
        self.content_lay.addWidget(w)

    def add_layout(self, lay):
        self.content_lay.addLayout(lay)

    def refit(self):
        # Пустой метод для обратной совместимости с вызовами в _apply_service_search
        pass

    def is_expanded(self) -> bool:
        # Для совместимости возвращаем True
        return True

    def set_expanded(self, expanded: bool, animate: bool = True):
        # Пустой метод для совместимости
        pass


class RoutingView(QWidget):
    # update_subscriptions_async завершается в worker-потоке DNS. Все изменения
    # Qt-виджетов прокидываем через сигнал, чтобы не трогать GUI из worker-а.
    _subscription_done = Signal(bool, int, bool)  # ok, count, clear_input

    def __init__(self):
        super().__init__()
        self._subscription_done.connect(self._on_subscription_done)
        self.engine = get_engine()
        self._route_restart_worker: _DnsRestartWorker | None = None
        self._route_restart_pending = False

        outer = QVBoxLayout(self)
        # справа/снизу нули: это место заняли поля GlowWrap (свечение панели)
        outer.setContentsMargins(24, 18, 0, 6)
        outer.setSpacing(14)

        # режимы и Start/Stop теперь в ГЛОБАЛЬНОЙ верхней панели (app.py),
        # доступной со всех вкладок — здесь их больше нет.

        self._favorite_services = get_favorite_services(list(SERVICES.keys()))
        self._service_search = ""

        # ── основная зона: слева список, справа профиль ──
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_left(), 1)
        body.addWidget(self._build_right())
        outer.addLayout(body, 1)

        self.refresh()

    # ════════════════ построение ════════════════
    def _build_left(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        title = QLabel("🚀  Сервисы")
        title.setStyleSheet(f"color:{theme.WHITE}; font-size:16px; font-weight:700;")
        lay.addWidget(title)
        self._service_search_input = QLineEdit()
        self._service_search_input.setPlaceholderText("🔍 Найти сервис: ChatGPT, GitHub, Discord...")
        self._service_search_input.setFixedHeight(34)
        self._service_search_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:9px;padding:0 11px;font-size:12px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}"
        )
        self._service_search_input.textChanged.connect(self._on_service_search)
        lay.addWidget(self._service_search_input)

        # ── телеграмизация: весь список сервисов рисует один paintEvent ──
        # (было: QScrollArea + ~150 дочерних виджетов, медленный ресайз)
        catalog = []
        for cat, (emoji, c1, c2) in CATEGORIES.items():
            catalog.append((cat, emoji, c1, c2,
                            [(svc, SERVICES[svc][1]) for svc in services_in_category(cat)]))
        self._canvas = ServiceCanvas(catalog)
        self._canvas.set_favorites(self._favorite_services)
        self._canvas.serviceToggled.connect(self._toggle_service)
        self._canvas.favoriteToggled.connect(self._toggle_favorite)
        self._canvas.categoryToggled.connect(self._toggle_category)
        lay.addWidget(self._canvas, 1)

        # ручной список
        lay.addWidget(self._build_manual())
        return wrap

    def _toggle_favorite(self, svc: str):
        if svc in self._favorite_services:
            self._favorite_services.remove(svc)
        else:
            self._favorite_services.append(svc)
        set_favorite_services(self._favorite_services, list(SERVICES.keys()))
        self._canvas.set_favorites(self._favorite_services)
        self.refresh()

    def _on_service_search(self, text: str):
        self._canvas.apply_search(text)

    def _toggle_category(self, cat: str, on: bool):
        """Включить/выключить все сервисы категории разом."""
        routed = self.engine.config.setdefault("routed_domains", [])
        for svc in services_in_category(cat):
            _, _, domains = SERVICES[svc]
            if on:
                for d in domains:
                    if d not in routed:
                        routed.append(d)
            else:
                routed[:] = [r for r in routed if r not in domains]
        self._apply()

    def _build_manual(self) -> QWidget:
        """Секция «Все активные домены» — показывает ВСЕ routed_domains,
        включая добавленные через книжку сервисов (AI, Медиа и т.д.).
        Пресетные отображаются с меткой сервиса, ручные — без метки.
        """
        sec = CategorySection("Все активные домены и процессы", "📋", theme.ACCENT, theme.ACCENT2)

        # ── компактная строка добавления ─────────────────────────────
        addrow = QHBoxLayout()
        addrow.setSpacing(8)
        self.add_input = QLineEdit()
        self.add_input.setPlaceholderText("chatgpt.com или chrome.exe")
        self.add_input.setFixedHeight(36)
        self.add_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;font-size:13px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        self.add_input.returnPressed.connect(self._add_typed)

        add_btn = QPushButton("+ Добавить")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setFixedHeight(36)
        add_btn.setStyleSheet(
            f"QPushButton{{background:{theme.grad(theme.GREEN, '#10b981')};color:{theme.WHITE};"
            "border:none;border-radius:8px;padding:0 14px;font-weight:600;font-size:13px;}}"
            f"QPushButton:hover{{border-radius:8px;}}")
        add_btn.clicked.connect(self._add_typed)

        pick_btn = QPushButton("🎮 Процесс")
        pick_btn.setCursor(Qt.PointingHandCursor)
        pick_btn.setFixedHeight(36)
        pick_btn.setToolTip("Выбрать запущенный процесс из списка")
        pick_btn.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;font-size:13px;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};}}")
        pick_btn.clicked.connect(self._pick_process)

        addrow.addWidget(self.add_input, 1)
        addrow.addWidget(pick_btn)
        addrow.addWidget(add_btn)
        sec.add_layout(addrow)

        # ── заголовок списка ──────────────────────────────────────────
        list_head = QHBoxLayout()
        list_title = QLabel("Активные записи")
        list_title.setStyleSheet(
            f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;border:none;"
        )
        self._manual_count = QLabel("—")
        self._manual_count.setStyleSheet(
            f"color:{theme.ACCENT3};font-size:11px;font-weight:700;background:transparent;border:none;"
        )
        list_head.addWidget(list_title)
        list_head.addStretch()
        list_head.addWidget(self._manual_count)
        sec.add_layout(list_head)

        # ── строка поиска ─────────────────────────────────────────────
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 Фильтр по домену...")
        self._search_input.setFixedHeight(30)
        self._search_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.SUBTEXT};"
            f"border:1px solid {theme.BORDER};border-radius:6px;padding:0 8px;font-size:12px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};color:{theme.TEXT};}}")
        self._search_input.textChanged.connect(self._on_search)
        sec.add_widget(self._search_input)

        # ── список ────────────────────────────────────────────────────
        # ── список: телеграмизация — один paintEvent вместо QScrollArea ──
        self._manual_canvas = ManualCanvas()
        self._manual_canvas.setMinimumHeight(330)
        self._manual_canvas.subscriptionRemoved.connect(self._remove_subscription)
        self._manual_canvas.serviceToggled.connect(self._toggle_service)
        self._manual_canvas.itemRemoved.connect(self._remove)
        sec.add_widget(self._manual_canvas)
        return sec

    def _on_search(self, text: str):
        """Фильтрует список по тексту — фильтрацию применяет сам канвас."""
        self._manual_canvas.apply_search(text)


    def _build_right(self) -> QWidget:
        # Рисуемая панель вместо QFrame+QSS (QSS-скругление стоило ~3.5 мс/кадр)
        panel = RoundedPanel(theme.CARD_DARK, theme.ACCENT, radius=18)
        panel.setFixedWidth(300)
        # Кэшированное свечение вместо QGraphicsDropShadowEffect (−3.7 мс/кадр):
        # эффект пересчитывал blur на каждом кадре, GlowWrap рисует готовый pixmap.
        # Поля подобраны так, чтобы панель осталась на прежнем месте, а
        # минимальная высота вкладки не выросла: справа/снизу свечение
        # заканчивается в прежних отступах вкладки (outer справа/снизу = 0).
        wrap = GlowWrap(panel, theme.ACCENT, blur=22, dy=6, radius=18,
                        margins=(32, 14, 24, 12))
        self._right_panel = wrap

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        # ── 0) ТАБЫ ДЛЯ COMBO РЕЖИМА ──
        self._tabs_widget = QWidget()
        self._tabs_widget.setStyleSheet("background:transparent;border:none;")
        self._tabs_lay = QHBoxLayout(self._tabs_widget)
        self._tabs_lay.setContentsMargins(0, 0, 0, 0)
        self._tabs_lay.setSpacing(6)

        self._dns_tab_btn = QPushButton("🔌 DNS")
        self._dns_tab_btn.setCursor(Qt.PointingHandCursor)
        self._dns_tab_btn.setFixedHeight(32)
        
        self._dpi_tab_btn = QPushButton("🛡 DPI")
        self._dpi_tab_btn.setCursor(Qt.PointingHandCursor)
        self._dpi_tab_btn.setFixedHeight(32)
        
        self._tabs_lay.addWidget(self._dns_tab_btn)
        self._tabs_lay.addWidget(self._dpi_tab_btn)
        lay.addWidget(self._tabs_widget)
        
        self._dns_tab_btn.clicked.connect(lambda: self._set_sidebar_tab("dns"))
        self._dpi_tab_btn.clicked.connect(lambda: self._set_sidebar_tab("dpi"))

        self._active_sidebar_tab = "dns"

        # ── 1) ФИКСИРОВАННАЯ КАРТОЧКА-ОКНО ДЛЯ СПИСКОВ (БЕЗ СВОРАЧИВАНИЯ) ──
        self._right_card = RoundedPanel(theme.CARD_DARK, theme.BORDER, radius=14)
        card_lay = QVBoxLayout(self._right_card)
        card_lay.setContentsMargins(12, 12, 12, 12)
        card_lay.setSpacing(10)

        # Заголовок карточки
        self._card_title = QLabel("🔌  Маршрут DNS")
        self._card_title.setStyleSheet(f"color:{theme.WHITE};font-size:14px;font-weight:700;background:transparent;border:none;")
        card_lay.addWidget(self._card_title)

        # Разделитель
        self._card_sep = QFrame()
        self._card_sep.setFixedHeight(1)
        self._card_sep.setStyleSheet(f"background:{theme.BORDER};border:none;")
        card_lay.addWidget(self._card_sep)

        # DNS Маршруты (TransportList)
        self._transport_list = TransportList()
        self._transport_list.transportChanged.connect(self._on_transport_change)
        card_lay.addWidget(self._transport_list)

        # DPI Стратегии (DpiStrategyList)
        from umbranet.widgets.dpi_strategy_list import DpiStrategyList
        self._dpi_strategy_list = DpiStrategyList()
        card_lay.addWidget(self._dpi_strategy_list)

        lay.addWidget(self._right_card)

        # ── 2) ДЕТАЛИ DNS ПРОФИЛЯ ──
        self._dns_profile_container = QWidget()
        self._dns_profile_container.setStyleSheet("background:transparent;border:none;")
        dns_prof_lay = QVBoxLayout(self._dns_profile_container)
        dns_prof_lay.setContentsMargins(0, 0, 0, 0)
        dns_prof_lay.setSpacing(8)

        self._prof_title = QLabel("🛡  —")
        self._prof_title.setStyleSheet(f"color:{theme.WHITE};font-size:14px;font-weight:700;background:transparent;border:none;")
        dns_prof_lay.addWidget(self._prof_title)

        self._prof_rows_host = QWidget()
        self._prof_rows_host.setStyleSheet("background:transparent;")
        self._prof_rows_lay = QVBoxLayout(self._prof_rows_host)
        self._prof_rows_lay.setContentsMargins(0, 0, 0, 0)
        self._prof_rows_lay.setSpacing(6)
        dns_prof_lay.addWidget(self._prof_rows_host)

        lay.addWidget(self._dns_profile_container)

        # растяжка — контент панели прижат к верху
        lay.addStretch()

        return wrap

    def _set_sidebar_tab(self, tab_key: str):
        self._active_sidebar_tab = tab_key
        
        # Обновляем стили кнопок вкладок
        active_style = (
            f"QPushButton{{"
            f"  background: {theme.ACCENT};"
            f"  color: {theme.WHITE};"
            f"  border: 1px solid {theme.ACCENT};"
            "  border-radius: 8px;"
            "  font-weight: bold;"
            "}}"
        )
        inactive_style = (
            f"QPushButton{{"
            f"  background: {theme.INPUT_BG};"
            f"  color: {theme.SUBTEXT};"
            f"  border: 1px solid {theme.BORDER};"
            "  border-radius: 8px;"
            "}}"
            f"QPushButton:hover{{"
            f"  border-color: {theme.ACCENT3};"
            f"  color: {theme.WHITE};"
            "}}"
        )
        
        if tab_key == "dns":
            self._dns_tab_btn.setStyleSheet(active_style)
            self._dpi_tab_btn.setStyleSheet(inactive_style)
            
            # Показываем DNS, скрываем DPI
            self._transport_list.setVisible(True)
            self._dpi_strategy_list.setVisible(False)
            self._card_title.setText("🔌  Маршрут DNS")
            self._dns_profile_container.setVisible(True)
        else:
            self._dns_tab_btn.setStyleSheet(inactive_style)
            self._dpi_tab_btn.setStyleSheet(active_style)
            
            # Показываем DPI, скрываем DNS
            self._transport_list.setVisible(False)
            self._dpi_strategy_list.setVisible(True)
            self._card_title.setText("🛡  Стратегия DPI")
            self._dns_profile_container.setVisible(False)

    def _on_transport_change(self, mode: str):
        """Транспорт сменился — обновляем конфиг и UI."""
        self.refresh()
        if self.engine.running:
            pass

    # ════════════════ логика ════════════════
    def _apply(self):
        save_config(self.engine.config)
        # Сбрасываем кеш списка — данные изменились
        self._manual_list_key = None
        # Сообщаем шапке что хостлист изменился — Старт может стать серым/активным
        try:
            from umbranet.engine_adapter import post_event
            post_event({"type": "config_changed", "section": "routing"})
        except Exception:
            pass
        # Мгновенно обновляем кнопку Старт в шапке (не ждём 200мс таймера)
        try:
            from PySide6.QtWidgets import QApplication
            for w in QApplication.topLevelWidgets():
                if hasattr(w, "_update_start_button"):
                    w._update_start_button()
                    break
        except Exception:
            pass

        if self.engine.running:
            self._restart_dns_after_route_change()
        else:
            self.engine.reload_config()
            self.refresh()

    def _restart_dns_after_route_change(self):
        """Перезапускает DNS после изменения маршрутов без смены системного DNS."""
        worker = getattr(self, "_route_restart_worker", None)
        if worker is not None and worker.isRunning():
            self._route_restart_pending = True
            return
        self._route_restart_pending = False
        self._route_restart_worker = _DnsRestartWorker(self.engine)
        self._route_restart_worker.done.connect(self._on_route_restart_done)
        self._route_restart_worker.start()

    def _on_route_restart_done(self, ok: bool):
        if not ok:
            log.warning("DNS restart после изменения маршрутов завершился с ошибкой")
        if self._route_restart_pending:
            self._restart_dns_after_route_change()
            return
        self.refresh()

    def _toggle_service(self, svc: str, on: bool):
        _, _, domains = SERVICES[svc]
        routed = self.engine.config.setdefault("routed_domains", [])
        if on:
            for d in domains:
                if d not in routed:
                    routed.append(d)
        else:
            routed[:] = [r for r in routed if r not in domains]
        self._apply()

    def _add_typed(self):
        raw_text = (self.add_input.text() or "").strip()
        if not raw_text:
            return

        # ── ПРОВЕРКА НА ПОДПИСКУ (URL) ──
        if raw_text.lower().startswith(("http://", "https://")):
            cfg = self.engine.config
            subs = cfg.setdefault("routed_subscriptions", [])
            if raw_text not in subs:
                subs.append(raw_text)
                save_config(cfg)
                self.add_input.setEnabled(False)
                self.add_input.setPlaceholderText("⏳  Загрузка подписки...")

                from umbranet.engine_adapter import update_subscriptions_async

                def on_done(ok, count):
                    self._subscription_done.emit(bool(ok), int(count), True)

                update_subscriptions_async(on_done)
                return

        import re
        tokens = re.split(r'[\s,;\n]+', raw_text)

        cfg = self.engine.config
        added_any = False

        for token in tokens:
            val = token.strip()
            if not val:
                continue

            for pre in ("https://", "http://", "www."):
                if val.lower().startswith(pre):
                    val = val[len(pre):]

            val = val.split("/")[0].strip().rstrip(".")
            if not val:
                continue

            key = "routed_processes" if val.lower().endswith(".exe") else "routed_domains"
            lst = cfg.setdefault(key, [])
            if val not in lst:
                lst.append(val)
                added_any = True

        self.add_input.clear()
        if added_any:
            self._apply()

    def _pick_process(self):
        dlg = ProcessPickerDialog(self)
        if dlg.exec() and dlg.result:
            name = dlg.result
            lst = self.engine.config.setdefault("routed_processes", [])
            if name not in lst:
                lst.append(name)
            self._apply()

    def _remove(self, name: str, key: str):
        # Защищаем дефолтные процессы — их удаление ломает per-app маршрутизацию
        PROTECTED_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe"}
        if key == "routed_processes" and name.lower() in PROTECTED_PROCESSES:
            # Тихо игнорируем — не даём удалить, чтобы не было проблем
            return
        if name in self.engine.config.get(key, []):
            self.engine.config[key].remove(name)
        self._apply()

    def _remove_subscription(self, url: str):
        cfg = self.engine.config
        subs = cfg.get("routed_subscriptions", [])
        if url in subs:
            subs.remove(url)
            save_config(cfg)

            self.add_input.setEnabled(False)
            self.add_input.setPlaceholderText("⏳  Удаление подписки...")

            from umbranet.engine_adapter import update_subscriptions_async

            def on_done(ok, count):
                self._subscription_done.emit(bool(ok), int(count), False)

            update_subscriptions_async(on_done)

    def _on_subscription_done(self, ok: bool, count: int, clear_input: bool):
        """Применяет результат обновления подписки в GUI-потоке."""
        self.add_input.setEnabled(True)
        if clear_input:
            self.add_input.clear()
        self.add_input.setPlaceholderText("chatgpt.com или chrome.exe")
        if not ok:
            log.warning("Обновление подписок завершилось без записи кэша")
        self._apply()

    # ════════════════ обновление ════════════════
    def refresh(self):
        cfg = self.engine.config

        # телеграмизация: состояния считает один канвас, анимируются
        # только реально изменившиеся тумблеры (никаких 26 виджетов).
        # Сервис включён, если ВСЕ его домены маршрутизируются
        # (поддомены учитывает логика ядра).
        service_states = {}
        for svc in SERVICES:
            _, _, domains = SERVICES[svc]
            service_states[svc] = bool(domains) and all(
                is_domain_routed(d, cfg) for d in domains)
        self._canvas.set_service_states(service_states)

        # ручной список (домены не из пресетов + процессы)
        self._rebuild_manual_list()

        # правая панель
        prof = get_active_dns_profile(cfg)
        self._prof_title.setText(f"🛡  {prof.get('name', '—')}")
        self._transport_list.refresh()
        self._rebuild_profile_rows(prof)

        # Переключение видимости в зависимости от текущего режима
        mode = get_current_mode()
        self._dpi_strategy_list.refresh()

        if mode == "dns_only":
            self._tabs_widget.setVisible(False)
            self._transport_list.setVisible(True)
            self._dpi_strategy_list.setVisible(False)
            self._card_title.setText("🔌  Маршрут DNS")
            self._dns_profile_container.setVisible(True)
        elif mode == "dpi_only":
            self._tabs_widget.setVisible(False)
            self._transport_list.setVisible(False)
            self._dpi_strategy_list.setVisible(True)
            self._card_title.setText("🛡  Стратегия DPI")
            self._dns_profile_container.setVisible(False)
        elif mode == "combo":
            self._tabs_widget.setVisible(True)
            self._set_sidebar_tab(self._active_sidebar_tab)

    # порядок и подписи параметров профиля для правой панели главного меню
    # (DoT/DoQ детали тут НЕ показываем — они нужны только в редакторе профиля)
    _PROFILE_FIELDS = [
        ("ipv4_primary", "IPv4 основной"),
        ("ipv4_secondary", "IPv4 резерв"),
        ("ipv6_primary", "IPv6 основной"),
        ("ipv6_secondary", "IPv6 резерв"),
        ("doh_url", "DoH URL"),
        ("dnscrypt_stamp", "DNSCrypt"),
    ]

    def _rebuild_profile_rows(self, prof: dict):
        # очистить прежние строки (виджеты-строки удаляются целиком)
        while self._prof_rows_lay.count():
            item = self._prof_rows_lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()
        # добавить все НЕпустые поля как отдельные виджеты-строки
        for key, label in self._PROFILE_FIELDS:
            val = prof.get(key)
            if val in (None, "", 0):
                continue
            row = QWidget(self._prof_rows_host)
            row.setStyleSheet("background:transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            k = QLabel(label)
            k.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
            v = QLabel(str(val))
            v.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:600;background:transparent;border:none;")
            v.setWordWrap(True)
            v.setAlignment(Qt.AlignRight | Qt.AlignTop)
            rl.addWidget(k, 0, Qt.AlignTop)
            rl.addStretch()
            rl.addWidget(v, 1)
            self._prof_rows_lay.addWidget(row)

    # Карта: домен -> имя сервиса (строится один раз из SERVICES)
    @staticmethod
    def _build_domain_to_service() -> dict:
        result = {}
        for svc, (_, _, domains) in SERVICES.items():
            for d in domains:
                result[d] = svc
        return result

    def _ensure_default_processes(self):
        """Гарантирует что chrome/msedge/firefox всегда в списке (защита от случайного удаления)."""
        try:
            cfg = self.engine.config
            procs = cfg.setdefault("routed_processes", [])
            for need in ("chrome.exe", "msedge.exe", "firefox.exe"):
                if need not in procs and need.lower() not in [x.lower() for x in procs]:
                    procs.append(need)
        except Exception:
            pass

    def _rebuild_manual_list(self, filter_text: str = ""):
        """Показывает ВСЕ активные домены, процессы и подписки.

        Телеграмизация: данные собираются как раньше (подписки -> домены ->
        процессы, домены пресетов с меткой сервиса), но рисует их один
        paintEvent (ManualCanvas) — ни одной карточки-QFrame на строку.
        Поиск, пустое состояние и «нет совпадений» канвас рисует сам.

        Кешируем последний набор данных — обновляем канвас только если
        содержимое реально изменилось (фильтр в канвасе, не здесь).
        """
        self._ensure_default_processes()
        cfg = self.engine.config
        domain_to_svc = self._build_domain_to_service()

        current_key = (
            tuple(sorted(cfg.get("routed_domains", []) or [])),
            tuple(sorted(cfg.get("routed_processes", []) or [])),
            tuple(sorted(cfg.get("routed_subscriptions", []) or [])),
        )
        if getattr(self, "_manual_list_key", None) == current_key:
            return  # содержимое не изменилось
        self._manual_list_key = current_key

        # Собираем записи: {name, key, icon, badge, badge_color, display}
        items = []

        # 1) Подписки — краткая подпись «хост / последняя часть пути»
        for sub_url in cfg.get("routed_subscriptions", []) or []:
            try:
                parts = sub_url.split("/")
                short_name = f"{parts[2]} / {parts[-1]}"
            except Exception:
                short_name = sub_url[:30] + "..."
            items.append({"name": sub_url, "key": "routed_subscriptions",
                          "icon": "📋", "badge": "", "badge_color": theme.ACCENT,
                          "display": short_name})

        # 2) Домены (пресетные — с меткой сервиса)
        for d in cfg.get("routed_domains", []) or []:
            svc = domain_to_svc.get(d)
            items.append({"name": d, "key": "routed_domains", "icon": "🌐",
                          "badge": svc or "", "badge_color": theme.ACCENT2 if svc else "",
                          "display": d})

        # 3) Процессы
        for pr in cfg.get("routed_processes", []) or []:
            items.append({"name": pr, "key": "routed_processes", "icon": "🎮",
                          "badge": "", "badge_color": "", "display": pr})

        # Сортировка: подписки (0), домены (1), процессы (2), затем по имени
        prio_map = {"routed_subscriptions": 0, "routed_domains": 1, "routed_processes": 2}
        items.sort(key=lambda it: (prio_map.get(it["key"], 1), (it["display"] or "").lower()))

        if hasattr(self, "_manual_count"):
            domains_n = len(cfg.get("routed_domains", []) or [])
            proc_n = len(cfg.get("routed_processes", []) or [])
            subs_n = len(cfg.get("routed_subscriptions", []) or [])
            self._manual_count.setText(f"{domains_n} дом. • {proc_n} проц. • {subs_n} под.")

        self._manual_canvas.set_items(items)

