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

from PySide6.QtCore import Qt, QThread, QTimer, Signal

log = logging.getLogger("UmbraNet.RoutingView")
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    get_active_dns_profile, get_engine, is_domain_routed, probe_doh, probe_host, save_config,
    get_latency_graph_settings, set_latency_graph_settings,
    get_favorite_services, set_favorite_services, get_current_mode,
)
from umbranet.services_catalog import (
    CATEGORIES, PRESET_DOMAINS, SERVICES, services_in_category,
)
from umbranet.widgets.collapsible import Collapsible
from umbranet.widgets.dialogs import ProcessPickerDialog
from umbranet.widgets.sparkline import Sparkline
from umbranet.widgets.toggle import Toggle
from umbranet.widgets.transport_list import TransportList


class _PingWorker(QThread):
    """Фоновый замер пинга для DNS и DPI (с обходом)."""
    done = Signal(bool, object, object)  # ok, dns_ms, dpi_ms

    def __init__(self, profile: dict, dns_mode: str, routed_domains: list, measure_dns: bool, measure_dpi: bool):
        super().__init__()
        self.profile = profile
        self.dns_mode = dns_mode
        self.routed_domains = routed_domains
        self.measure_dns = measure_dns
        self.measure_dpi = measure_dpi

    def run(self):
        dns_ms = None
        dpi_ms = None

        # 1. Замеряем DNS пинг
        if self.measure_dns:
            if self.dns_mode == "doh" and self.profile.get("doh_url"):
                ok, ms = probe_doh(self.profile["doh_url"])
            elif self.profile.get("ipv4_primary"):
                ok, ms = probe_host(self.profile["ipv4_primary"])
            else:
                ok, ms = False, None
            if ok:
                dns_ms = ms

        # 2. Замеряем DPI пинг (через TCP 443 к обходящему домену)
        if self.measure_dpi:
            target_domain = "google.com"
            if self.routed_domains:
                # Ищем популярный обходящий домен, чтобы замерить реальный обход
                for d in self.routed_domains:
                    if "google" in d or "youtube" in d or "discord" in d:
                        target_domain = d
                        break
                else:
                    target_domain = self.routed_domains[0]
            
            # Коннект на порт 443 пройдет через WinDivert и применит активную стратегию
            ok, ms = probe_host(target_domain, 443)
            if ok:
                dpi_ms = ms

        self.done.emit(True, dns_ms, dpi_ms)


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


class _NoWheelComboBox(QComboBox):
    """ComboBox без случайного переключения колесом мыши."""

    def wheelEvent(self, event):
        event.ignore()


class _NoWheelSlider(QSlider):
    """Слайдер настроек без случайного изменения колесом мыши."""

    def wheelEvent(self, event):
        event.ignore()


class LatencyGraphSettingsDialog(QDialog):
    """Настройки графиков пинга сети."""

    def __init__(self, current: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки графиков")
        self.setMinimumWidth(420)
        self.result = dict(current or {})
        self.setStyleSheet(f"QDialog{{background:{theme.BG};}}")
        self._build(current or {})

    def _build(self, cur: dict):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("📈  Настройки графиков пинга")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:16px;font-weight:700;background:transparent;border:none;")
        root.addWidget(title)

        # Вид графика
        row = QHBoxLayout()
        lbl = QLabel("Вид графика")
        lbl.setFixedWidth(150)
        lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        self._mode = _NoWheelComboBox()
        self._mode.addItem("Биржевые палочки", "bars")
        self._mode.addItem("Плавная линия", "smooth")
        self._mode.addItem("Угловатая линия", "angular")
        idx = self._mode.findData(cur.get("mode", "bars"))
        if idx >= 0:
            self._mode.setCurrentIndex(idx)
        self._mode.setStyleSheet(self._combo_qss())
        row.addWidget(lbl)
        row.addWidget(self._mode, 1)
        root.addLayout(row)

        self._grid_value = QLabel()
        self._grid = self._slider_row(root, "Размер сетки", int(cur.get("grid", 5)), 2, 10, self._grid_value)

        self._interval_value = QLabel()
        self._interval = self._slider_row(root, "Частота обновления", int(cur.get("interval_ms", 2000)) // 500, 2, 30, self._interval_value)

        self._height_value = QLabel()
        self._height = self._slider_row(root, "Высота графиков", int(cur.get("height", 140)), 90, 240, self._height_value)

        self._refresh_labels()
        self._grid.valueChanged.connect(lambda _=0: self._refresh_labels())
        self._interval.valueChanged.connect(lambda _=0: self._refresh_labels())
        self._height.valueChanged.connect(lambda _=0: self._refresh_labels())

        hint = QLabel("Рекомендуется интервал 2 секунды, чтобы не перегружать сетевой стек.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;")
        root.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.setFixedHeight(34)
        cancel.setStyleSheet(self._btn_qss(theme.CARD, theme.TEXT, border=True))
        cancel.clicked.connect(self.reject)
        ok = QPushButton("✓ Применить")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setFixedHeight(34)
        ok.setStyleSheet(self._btn_qss(theme.ACCENT, theme.WHITE))
        ok.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        root.addLayout(buttons)

    def _slider_row(self, parent, title: str, value: int, mn: int, mx: int, value_label: QLabel) -> QSlider:
        row = QHBoxLayout()
        lbl = QLabel(title)
        lbl.setFixedWidth(150)
        lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        slider = _NoWheelSlider(Qt.Horizontal)
        slider.setRange(mn, mx)
        slider.setValue(max(mn, min(mx, value)))
        slider.setMinimumHeight(30)
        slider.setStyleSheet(
            f"QSlider{{background:transparent;}}"
            f"QSlider::groove:horizontal{{"
            "height:8px;background:rgba(255,255,255,0.10);"
            f"border:1px solid {theme.BORDER};border-radius:4px;"
            "}}"
            f"QSlider::sub-page:horizontal{{"
            f"background:{theme.grad(theme.ACCENT, theme.ACCENT3)};"
            "border-radius:4px;"
            "}}"
            f"QSlider::add-page:horizontal{{"
            "background:rgba(255,255,255,0.045);border-radius:4px;"
            "}}"
            f"QSlider::handle:horizontal{{"
            "width:20px;height:20px;margin:-7px 0;"
            f"background:{theme.ACCENT3};border:2px solid {theme.WHITE};"
            "border-radius:10px;"
            "}}"
            f"QSlider::handle:horizontal:hover{{background:{theme.WHITE};border-color:{theme.ACCENT3};}}"
        )
        value_label.setFixedWidth(70)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value_label.setStyleSheet(f"color:{theme.ACCENT3};font-size:12px;font-family:Consolas;background:transparent;border:none;")
        row.addWidget(lbl)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        parent.addLayout(row)
        return slider

    def _refresh_labels(self):
        self._grid_value.setText(f"{self._grid.value()}x")
        self._interval_value.setText(f"{self._interval.value() * 500} мс")
        self._height_value.setText(f"{self._height.value()} px")

    def _accept(self):
        self.result = {
            "mode": self._mode.currentData(),
            "grid": self._grid.value(),
            "interval_ms": self._interval.value() * 500,
            "height": self._height.value(),
        }
        self.accept()

    def _combo_qss(self):
        return (
            f"QComboBox{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;min-height:32px;}}"
            f"QComboBox:hover{{border-color:{theme.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{theme.CARD};color:{theme.TEXT};"
            f"selection-background-color:{theme.ACCENT};border:1px solid {theme.BORDER};}}"
        )

    def _btn_qss(self, bg, fg, border=False):
        b = f"border:1px solid {theme.BORDER};" if border else "border:none;"
        return f"QPushButton{{background:{bg};color:{fg};{b}border-radius:9px;padding:0 14px;font-weight:600;}}"


class RoutingView(QWidget):
    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self._route_restart_worker: _DnsRestartWorker | None = None
        self._route_restart_pending = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        # режимы и Start/Stop теперь в ГЛОБАЛЬНОЙ верхней панели (app.py),
        # доступной со всех вкладок — здесь их больше нет.

        self._service_toggles: dict[str, Toggle] = {}
        self._favorite_toggles: dict[str, Toggle] = {}
        self._service_rows: dict[str, QFrame] = {}
        self._favorite_rows: dict[str, QFrame] = {}
        self._category_sections: dict[str, Collapsible] = {}
        self._favorite_services = get_favorite_services(list(SERVICES.keys()))
        self._service_search = ""

        # ── основная зона: слева список, справа профиль ──
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_left(), 1)
        body.addWidget(self._build_right())
        outer.addLayout(body, 1)

        self._build_service_cards()
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

        # прокручиваемая область с карточками категорий
        self._cards_holder = QVBoxLayout()
        self._cards_holder.setSpacing(12)
        cards_wrap = QWidget()
        cards_wrap.setLayout(self._cards_holder)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # ВАЖНО: AlwaysOn здесь не про «всегда показывать ползунок»,
        # а про постоянное резервирование ширины под него. Иначе при раскрытии
        # книжки (например AI/ChatGPT) вертикальный scrollbar появляется
        # динамически, viewport становится уже, и правая часть карточек
        # визуально «съезжает». Когда прокрутка не нужна, disabled-handle
        # в theme.scrollbar_qss() прозрачный.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss()
        )
        scroll.setWidget(cards_wrap)
        lay.addWidget(scroll, 1)

        # ручной список
        lay.addWidget(self._build_manual())
        return wrap

    def _build_service_cards(self):
        # Избранное — отдельная статическая секция сверху. В ней дублируются строки сервисов,
        # а состояния тумблеров синхронизируются в refresh().
        self._favorite_section = CategorySection("Избранное", "⭐", theme.YELLOW, theme.ACCENT2)
        self._favorite_section.setVisible(bool(self._favorite_services))
        self._cards_holder.addWidget(self._favorite_section)
        self._rebuild_favorites_section()

        # Каждая категория — статическая секция с тумблером в заголовке.
        # Тумблер включает/выключает ВСЮ категорию.
        self._category_toggles = {}
        for cat, (emoji, c1, c2) in CATEGORIES.items():
            cat_toggle = Toggle()
            cat_toggle.toggled.connect(lambda on, c=cat: self._toggle_category(c, on))
            self._category_toggles[cat] = cat_toggle

            sec = CategorySection(cat, emoji, c1, c2, toggle=cat_toggle)
            self._category_sections[cat] = sec
            for svc in services_in_category(cat):
                row = self._make_service_row(svc, favorite_context=False)
                self._service_rows[svc] = row
                sec.add_widget(row)
            self._cards_holder.addWidget(sec)
        self._cards_holder.addStretch()

    def _make_service_row(self, svc: str, favorite_context: bool = False) -> QFrame:
        _, semoji, _domains = SERVICES[svc]
        row = QFrame()
        row.setProperty("serviceRow", True)
        row.setStyleSheet(
            f"QFrame[serviceRow='true']{{background:transparent;border-radius:8px;}}"
            f"QFrame[serviceRow='true']:hover{{background:rgba(255,255,255,0.045);}}"
        )
        row.setObjectName(svc)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(8, 3, 6, 3)
        rl.setSpacing(7)

        star = QPushButton("★" if svc in self._favorite_services else "☆")
        star.setCursor(Qt.PointingHandCursor)
        star.setFixedSize(22, 22)
        star.setToolTip("Убрать из избранного" if svc in self._favorite_services else "Добавить в избранное")
        star.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{theme.YELLOW if svc in self._favorite_services else theme.MUTED};"
            "font-size:15px;font-weight:700;}}"
            f"QPushButton:hover{{color:{theme.YELLOW};}}"
        )
        star.clicked.connect(lambda _=False, s=svc: self._toggle_favorite(s))
        rl.addWidget(star)

        si = QLabel(semoji)
        si.setFixedWidth(28)
        si.setAlignment(Qt.AlignCenter)
        si.setStyleSheet("background:transparent;border:none;font-size:16px;padding-right:3px;")
        rl.addWidget(si)

        lbl = QLabel(svc)
        lbl.setStyleSheet(f"color:{theme.TEXT};font-size:13px;background:transparent;border:none;")
        rl.addWidget(lbl, 1)

        tg = Toggle()
        tg.toggled.connect(lambda on, s=svc: self._toggle_service(s, on))
        if favorite_context:
            self._favorite_toggles[svc] = tg
        else:
            self._service_toggles[svc] = tg
        rl.addWidget(tg)
        return row

    def _rebuild_favorites_section(self):
        if not hasattr(self, "_favorite_section"):
            return
        while self._favorite_section._body_lay.count():
            item = self._favorite_section._body_lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()
        self._favorite_rows.clear()
        self._favorite_toggles.clear()
        for svc in self._favorite_services:
            if svc in SERVICES:
                row = self._make_service_row(svc, favorite_context=True)
                self._favorite_rows[svc] = row
                self._favorite_section.add_widget(row)
        self._favorite_section.setVisible(bool(self._favorite_services))
        self._favorite_section.refit()

    def _toggle_favorite(self, svc: str):
        if svc in self._favorite_services:
            self._favorite_services.remove(svc)
        else:
            self._favorite_services.append(svc)
        set_favorite_services(self._favorite_services, list(SERVICES.keys()))
        self._rebuild_favorites_section()
        self._build_service_cards_refresh_only()
        self._apply_service_search()
        self.refresh()

    def _build_service_cards_refresh_only(self):
        # Обновляет внешний вид звёзд без пересоздания всей страницы.
        for svc, row in self._service_rows.items():
            star = row.findChild(QPushButton)
            if star:
                fav = svc in self._favorite_services
                star.setText("★" if fav else "☆")
                star.setToolTip("Убрать из избранного" if fav else "Добавить в избранное")
                star.setStyleSheet(
                    f"QPushButton{{background:transparent;border:none;color:{theme.YELLOW if fav else theme.MUTED};"
                    "font-size:15px;font-weight:700;}}"
                    f"QPushButton:hover{{color:{theme.YELLOW};}}"
                )

    def _on_service_search(self, text: str):
        self._service_search = (text or "").strip().lower()
        self._apply_service_search()

    def _apply_service_search(self):
        q = self._service_search
        # Избранное фильтруем тоже.
        for svc, row in self._favorite_rows.items():
            row.setVisible((not q) or (q in svc.lower()))
        if hasattr(self, "_favorite_section"):
            any_fav = any(row.isVisible() for row in self._favorite_rows.values())
            self._favorite_section.setVisible(bool(self._favorite_services) and ((not q) or any_fav))
            self._favorite_section.refit()

        for cat, sec in self._category_sections.items():
            cat_match = q and q in cat.lower()
            any_match = False
            for svc in services_in_category(cat):
                row = self._service_rows.get(svc)
                if row is None:
                    continue
                match = (not q) or cat_match or (q in svc.lower())
                row.setVisible(match)
                any_match = any_match or match
            sec.setVisible(any_match)
            if q and any_match and not sec.is_expanded():
                sec.set_expanded(True, animate=True)
            sec.refit()

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
        self._manual_list = QVBoxLayout()
        self._manual_list.setSpacing(6)
        self._list_container = QWidget()   # сохраняем как родителя для строк
        self._list_container.setLayout(self._manual_list)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Стабильная ширина списка: место под scrollbar зарезервировано всегда,
        # поэтому строки не прыгают при появлении/исчезновении прокрутки.
        sc.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        sc.setMinimumHeight(330)
        sc.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            + theme.scrollbar_qss())
        sc.setWidget(self._list_container)
        sec.add_widget(sc)
        return sec

    def _on_search(self, text: str):
        """Фильтрует список по введённому тексту без пересоздания виджетов."""
        q = text.strip().lower()
        for i in range(self._manual_list.count()):
            item = self._manual_list.itemAt(i)
            w = item.widget() if item else None
            if w is None:
                continue
            # имя домена храним в objectName строки
            name = w.objectName()
            visible = (not q) or (q in name.lower())
            w.setVisible(visible)

    def _build_right(self) -> QWidget:
        panel = QFrame()
        panel.setFixedWidth(300)
        panel.setStyleSheet(
            f"QFrame{{"
            f"  background: {theme.CARD_DARK};"
            f"  border: 1px solid {theme.ACCENT};"
            "  border-radius: 18px;"
            "}}"
        )
        theme.glow(panel, theme.ACCENT, blur=22, dy=6)
        self._right_panel = panel

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
        self._right_card = QFrame()
        self._right_card.setStyleSheet(
            f"QFrame{{"
            f"  background: {theme.CARD_DARK};"
            f"  border: 1px solid {theme.BORDER};"
            "  border-radius: 14px;"
            "}}"
        )
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
        self._dpi_strategy_list.strategyChanged.connect(lambda _: QTimer.singleShot(100, self._do_ping))
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

        # растяжка ДО латентности — чтобы график был всегда ПРИЖАТ К НИЗУ
        lay.addStretch()

        # разделитель
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{theme.BORDER};border:none;")
        lay.addWidget(sep)

        # ── Заголовок области пинга сети ──
        lat_head = QHBoxLayout()
        self._lat_title = QLabel("📈  Пинг")
        self._lat_title.setStyleSheet(f"color:{theme.WHITE};font-size:13px;font-weight:700;background:transparent;border:none;")
        lat_head.addWidget(self._lat_title)
        lat_head.addStretch()
        
        self._graph_btn = QPushButton("⚙︎")
        self._graph_btn.setCursor(Qt.PointingHandCursor)
        self._graph_btn.setFixedSize(30, 26)
        self._graph_btn.setToolTip("Настройки графиков")
        self._graph_btn.setStyleSheet(
            f"QPushButton{{background:{theme.INPUT_BG};color:{theme.ACCENT3};"
            f"border:1px solid {theme.BORDER};border-radius:8px;"
            "font-family:'Segoe UI Symbol';font-size:15px;font-weight:700;"
            "padding-bottom:1px;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT3};color:{theme.WHITE};background:{theme.CARD};}}"
        )
        
        self._graph_clear_btn = QPushButton("🧹")
        self._graph_clear_btn.setCursor(Qt.PointingHandCursor)
        self._graph_clear_btn.setFixedSize(30, 26)
        self._graph_clear_btn.setToolTip("Очистить графики")
        self._graph_clear_btn.setStyleSheet(
            f"QPushButton{{background:{theme.INPUT_BG};color:{theme.SUBTEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;"
            "font-size:13px;font-weight:700;padding-bottom:1px;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT3};color:{theme.WHITE};background:{theme.CARD};}}"
        )
        self._graph_clear_btn.clicked.connect(self._clear_ping_graph)
        lat_head.addWidget(self._graph_clear_btn)

        self._graph_btn.clicked.connect(self._open_graph_settings)
        lat_head.addWidget(self._graph_btn)
        lay.addLayout(lat_head)

        self._graph_settings = get_latency_graph_settings()
        h = int(self._graph_settings.get("height", 140))

        # ── 1) Контейнер для Пинга DPI (Стратегия) ──
        self._dpi_graph_container = QWidget()
        self._dpi_graph_container.setStyleSheet("background:transparent;border:none;")
        dpi_g_lay = QVBoxLayout(self._dpi_graph_container)
        dpi_g_lay.setContentsMargins(0, 0, 0, 0)
        dpi_g_lay.setSpacing(4)

        dpi_lbl = QLabel("🛡 Пинг DPI (Стратегия)")
        dpi_lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:10px;font-weight:bold;background:transparent;")
        dpi_g_lay.addWidget(dpi_lbl)

        self._dpi_spark = Sparkline(
            capacity=40,
            mode=self._graph_settings.get("mode", "bars"),
            grid_size=int(self._graph_settings.get("grid", 5)),
        )
        self._dpi_spark.setFixedHeight(h)
        dpi_g_lay.addWidget(self._dpi_spark)

        dpi_row = QHBoxLayout()
        self._dpi_lat_cur = QLabel("Текущий: —")
        self._dpi_lat_cur.setStyleSheet(f"color:{theme.ACCENT};font-size:11px;background:transparent;border:none;")
        self._dpi_lat_avg = QLabel("Сред.: —")
        self._dpi_lat_avg.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;background:transparent;border:none;")
        dpi_row.addWidget(self._dpi_lat_cur)
        dpi_row.addStretch()
        dpi_row.addWidget(self._dpi_lat_avg)
        dpi_g_lay.addLayout(dpi_row)

        lay.addWidget(self._dpi_graph_container)

        # ── 2) Контейнер для Пинга DNS (Маршрут) ──
        self._dns_graph_container = QWidget()
        self._dns_graph_container.setStyleSheet("background:transparent;border:none;")
        dns_g_lay = QVBoxLayout(self._dns_graph_container)
        dns_g_lay.setContentsMargins(0, 0, 0, 0)
        dns_g_lay.setSpacing(4)

        dns_lbl = QLabel("🔌 Пинг DNS (Маршрут)")
        dns_lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:10px;font-weight:bold;background:transparent;")
        dns_g_lay.addWidget(dns_lbl)

        self._dns_spark = Sparkline(
            capacity=40,
            mode=self._graph_settings.get("mode", "bars"),
            grid_size=int(self._graph_settings.get("grid", 5)),
        )
        self._dns_spark.setFixedHeight(h)
        dns_g_lay.addWidget(self._dns_spark)

        dns_row = QHBoxLayout()
        self._dns_lat_cur = QLabel("Текущий: —")
        self._dns_lat_cur.setStyleSheet(f"color:{theme.ACCENT2};font-size:11px;background:transparent;border:none;")
        self._dns_lat_avg = QLabel("Сред.: —")
        self._dns_lat_avg.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;background:transparent;border:none;")
        dns_row.addWidget(self._dns_lat_cur)
        dns_row.addStretch()
        dns_row.addWidget(self._dns_lat_avg)
        dns_g_lay.addLayout(dns_row)

        lay.addWidget(self._dns_graph_container)

        # Таймер опроса пингов
        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(int(self._graph_settings.get("interval_ms", 2000)))
        self._ping_timer.timeout.connect(self._do_ping)
        self._ping_timer.start()
        QTimer.singleShot(400, self._do_ping)

        return panel

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
        QTimer.singleShot(300, self._do_ping)

    def _clear_ping_graph(self):
        """Очищает историю графиков пинга вручную."""
        self._dns_spark.set_data([])
        self._dpi_spark.set_data([])
        self._dns_lat_cur.setText("Текущий: —")
        self._dns_lat_avg.setText("Сред.: —")
        self._dpi_lat_cur.setText("Текущий: —")
        self._dpi_lat_avg.setText("Сред.: —")
        QTimer.singleShot(150, self._do_ping)

    def _open_graph_settings(self):
        dlg = LatencyGraphSettingsDialog(self._graph_settings, self)
        if not dlg.exec():
            return
        self._graph_settings = dlg.result
        set_latency_graph_settings(self._graph_settings)
        
        mode = self._graph_settings.get("mode", "bars")
        grid = int(self._graph_settings.get("grid", 5))
        self._dns_spark.set_mode(mode)
        self._dns_spark.set_grid_size(grid)
        self._dpi_spark.set_mode(mode)
        self._dpi_spark.set_grid_size(grid)
        
        self._ping_timer.setInterval(int(self._graph_settings.get("interval_ms", 2000)))
        self.refresh()

    def _do_ping(self):
        if getattr(self, "_ping_worker", None) and self._ping_worker.isRunning():
            return
            
        mode = get_current_mode()
        measure_dns = (mode in ("dns_only", "combo"))
        measure_dpi = (mode in ("dpi_only", "combo"))

        prof = get_active_dns_profile(self.engine.config)
        dns_mode = self.engine.config.get("xbox_dns_mode", "udp")
        routed_domains = self.engine.config.get("routed_domains", [])

        self._ping_worker = _PingWorker(prof, dns_mode, routed_domains, measure_dns, measure_dpi)
        self._ping_worker.done.connect(self._on_ping_done)
        self._ping_worker.start()

    def _on_ping_done(self, ok: bool, dns_ms, dpi_ms):
        if dns_ms is not None:
            self._dns_spark.push(dns_ms)
            self._dns_lat_cur.setText(f"Текущий: {dns_ms} мс")
            avg = self._dns_spark.avg
            self._dns_lat_avg.setText(f"Сред.: {int(avg)} мс" if avg else "Сред.: —")
            
        if dpi_ms is not None:
            self._dpi_spark.push(dpi_ms)
            self._dpi_lat_cur.setText(f"Текущий: {dpi_ms} мс")
            avg = self._dpi_spark.avg
            self._dpi_lat_avg.setText(f"Сред.: {int(avg)} мс" if avg else "Сред.: —")

    # ════════════════ логика ════════════════
    def _apply(self):
        save_config(self.engine.config)
        # Сбрасываем кеш списка — данные изменились
        self._manual_list_key = None

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
                    self.add_input.setEnabled(True)
                    self.add_input.clear()
                    self.add_input.setPlaceholderText("chatgpt.com или chrome.exe")
                    self._apply()

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
                self.add_input.setEnabled(True)
                self.add_input.setPlaceholderText("chatgpt.com или chrome.exe")
                self._apply()

            update_subscriptions_async(on_done)

    # ════════════════ обновление ════════════════
    def refresh(self):
        cfg = self.engine.config
        routed = set(cfg.get("routed_domains", []))

        # тумблеры сервисов: сервис включён, если ВСЕ его домены
        # маршрутизируются (учитываем поддомены через логику ядра —
        # ядро убирает избыточные поддомены при сохранении).
        service_states = {}
        for svc, tg in self._service_toggles.items():
            _, _, domains = SERVICES[svc]
            on = bool(domains) and all(is_domain_routed(d, cfg) for d in domains)
            service_states[svc] = on
            tg.blockSignals(True)
            tg.setChecked(on, animate=False)   # без анимации — иначе 26 анимаций сразу
            tg.blockSignals(False)

        for svc, tg in getattr(self, "_favorite_toggles", {}).items():
            on = service_states.get(svc, False)
            tg.blockSignals(True)
            tg.setChecked(on, animate=False)
            tg.blockSignals(False)

        # тумблеры категорий: вкл (все), выкл (никто), partial/оранжевый (часть)
        for cat, ctg in getattr(self, "_category_toggles", {}).items():
            svcs = services_in_category(cat)
            states = [service_states.get(s, False) for s in svcs]
            ctg.blockSignals(True)
            if svcs and all(states):
                ctg.setChecked(True, animate=False)
            elif any(states):
                ctg.setPartial(animate=False)
            else:
                ctg.setChecked(False, animate=False)
            ctg.blockSignals(False)

        self._apply_service_search()

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

        # Загружаем базовую высоту из настроек
        h = int(self._graph_settings.get("height", 140))

        if mode == "dns_only":
            self._tabs_widget.setVisible(False)
            self._transport_list.setVisible(True)
            self._dpi_strategy_list.setVisible(False)
            self._card_title.setText("🔌  Маршрут DNS")
            self._dns_profile_container.setVisible(True)
            
            # Один график DNS: полная высота
            self._dns_spark.setFixedHeight(h)
            self._dns_graph_container.setVisible(True)
            self._dpi_graph_container.setVisible(False)
            self._lat_title.setText("📈  Пинг DNS")
        elif mode == "dpi_only":
            self._tabs_widget.setVisible(False)
            self._transport_list.setVisible(False)
            self._dpi_strategy_list.setVisible(True)
            self._card_title.setText("🛡  Стратегия DPI")
            self._dns_profile_container.setVisible(False)
            
            # Один график DPI: полная высота
            self._dpi_spark.setFixedHeight(h)
            self._dns_graph_container.setVisible(False)
            self._dpi_graph_container.setVisible(True)
            self._lat_title.setText("📈  Пинг DPI")
        elif mode == "combo":
            self._tabs_widget.setVisible(True)
            self._set_sidebar_tab(self._active_sidebar_tab)
            
            # Два графика: делим базовую высоту пополам
            combo_h = max(60, h // 2)
            self._dns_spark.setFixedHeight(combo_h)
            self._dpi_spark.setFixedHeight(combo_h)
            self._dns_graph_container.setVisible(True)
            self._dpi_graph_container.setVisible(True)
            self._lat_title.setText("📈  Пинг сети")

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

    def _rebuild_manual_list(self, filter_text: str = ""):
        """Показывает ВСЕ активные домены, процессы и подписки.

        Домены из пресетов (книжка AI / Медиа / Разное) отображаются
        с меткой сервиса. Ручные домены — без метки.
        Кнопка ✕ у пресетных доменов выключает весь сервис через тумблер,
        у ручных — просто удаляет запись.

        Кешируем последний набор данных — пересоздаём виджеты только если
        содержимое реально изменилось. Это убирает visual flash при каждом
        refresh() когда список не менялся.
        """
        cfg = self.engine.config
        domain_to_svc = self._build_domain_to_service()

        # Строим текущий ключ состояния
        current_key = (
            tuple(sorted(cfg.get("routed_domains", []))),
            tuple(sorted(cfg.get("routed_processes", []))),
            tuple(sorted(cfg.get("routed_subscriptions", []))),
            filter_text,
        )
        if getattr(self, "_manual_list_key", None) == current_key:
            return  # содержимое не изменилось — не трогаем виджеты
        self._manual_list_key = current_key

        # Содержимое изменилось — очищаем и перестраиваем
        while self._manual_list.count():
            item = self._manual_list.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()

        # Собираем все записи: (name_or_url, config_key, icon, badge_text, badge_color, display_name)
        items = []

        # 1) Подписки
        for sub_url in cfg.get("routed_subscriptions", []):
            try:
                parts = sub_url.split("/")
                short_name = f"{parts[2]} / {parts[-1]}"
            except Exception:
                short_name = sub_url[:30] + "..."
            items.append((sub_url, "routed_subscriptions", "📋", "подписка", theme.ACCENT, short_name))

        # 2) Домены
        for d in cfg.get("routed_domains", []):
            svc = domain_to_svc.get(d)
            if svc:
                items.append((d, "routed_domains", "🌐", svc, theme.ACCENT2, d))
            else:
                items.append((d, "routed_domains", "🌐", "", "", d))

        # 3) Процессы
        for p in cfg.get("routed_processes", []):
            items.append((p, "routed_processes", "🎮", "", "", p))

        # Сортировка: подписки (0), домены (1), процессы (2)
        def sort_priority(item):
            key = item[1]
            prio = 1
            if key == "routed_subscriptions":
                prio = 0
            elif key == "routed_processes":
                prio = 2
            return prio, item[5].lower()

        items.sort(key=sort_priority)

        if hasattr(self, "_manual_count"):
            domains_n = len(cfg.get("routed_domains", []) or [])
            proc_n = len(cfg.get("routed_processes", []) or [])
            subs_n = len(cfg.get("routed_subscriptions", []) or [])
            self._manual_count.setText(f"{domains_n} дом. • {proc_n} проц. • {subs_n} под.")

        # Пустое состояние
        if not items:
            empty = QLabel(
                "Нет активных доменов. Включите сервисы выше или добавьте домен вручную.",
                self._list_container,
            )
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                f"color:{theme.MUTED};font-size:12px;background:transparent;border:none;"
            )
            self._manual_list.addWidget(empty)
            self._manual_list.addStretch()
            return

        q = filter_text.strip().lower()
        any_visible = False

        for name, key, icon, badge, badge_color, display_name in items:
            visible = (not q) or (q in display_name.lower()) or (q in name.lower())

            # Передаём _list_container как родителя — без этого Qt на Windows
            # кратко показывает QFrame как top-level окно (белый прямоугольник)
            row = QFrame(self._list_container)
            row.setObjectName(name)
            row.setVisible(visible)
            if visible:
                any_visible = True

            # Карточка строки. Используем property selector, а не QFrame{...},
            # чтобы стиль не протекал на вложенные QLabel (они тоже QFrame в Qt).
            row.setProperty("routeRow", True)
            row.setMinimumHeight(36)
            row.setStyleSheet(
                f"QFrame[routeRow='true']{{"
                "background:rgba(255,255,255,0.035);"
                f"border:1px solid {theme.BORDER};"
                "border-radius:10px;"
                "}}"
                f"QFrame[routeRow='true']:hover{{"
                "background:rgba(139,109,255,0.10);"
                f"border-color:{theme.ACCENT};"
                "}}"
            )

            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 6, 8, 6)
            rl.setSpacing(8)

            # иконка
            ico_lbl = QLabel(icon)
            ico_lbl.setFixedWidth(22)
            ico_lbl.setAlignment(Qt.AlignCenter)
            ico_lbl.setStyleSheet("background:transparent;border:none;font-size:14px;")
            rl.addWidget(ico_lbl)

            kind_text = {
                "routed_subscriptions": "подписка",
                "routed_domains": "домен",
                "routed_processes": "процесс",
            }.get(key, "запись")
            kind_color = {
                "routed_subscriptions": theme.ACCENT,
                "routed_domains": theme.ACCENT2,
                "routed_processes": theme.ORANGE,
            }.get(key, theme.MUTED)
            kind = QLabel(kind_text)
            kind.setFixedWidth(66)
            kind.setAlignment(Qt.AlignCenter)
            kind.setStyleSheet(
                f"color:{kind_color};font-size:10px;font-weight:700;"
                f"background:rgba(255,255,255,0.035);border:1px solid {kind_color};"
                "border-radius:8px;padding:2px 6px;"
            )
            rl.addWidget(kind)

            # отображаемое имя (короткое имя подписки, домен или процесс)
            name_lbl = QLabel(display_name)
            name_lbl.setStyleSheet(
                f"color:{theme.TEXT};font-size:12px;font-family:Consolas;"
                "background:transparent;border:none;"
            )
            rl.addWidget(name_lbl, 1)

            # бейдж сервиса
            if badge and key != "routed_subscriptions":
                b = QLabel(badge)
                b.setStyleSheet(
                    f"color:{badge_color};font-size:10px;font-weight:700;"
                    "background:transparent;border:none;"
                )
                rl.addWidget(b)

            # кнопка удаления
            rm = QPushButton("✕")
            rm.setCursor(Qt.PointingHandCursor)
            rm.setFixedSize(20, 20)
            rm.setToolTip(
                "Удалить подписку" if key == "routed_subscriptions"
                else (f"Выключить сервис «{badge}»" if badge
                      else f"Удалить {display_name}")
            )
            rm.setStyleSheet(
                f"QPushButton{{background:transparent;color:{theme.MUTED};"
                "border:none;font-size:12px;}}"
                f"QPushButton:hover{{color:{theme.RED};}}"
            )
            if key == "routed_subscriptions":
                rm.clicked.connect(
                    lambda _=False, u=name: self._remove_subscription(u)
                )
            elif badge:
                rm.clicked.connect(
                    lambda _=False, s=badge: self._toggle_service(s, False)
                )
            else:
                rm.clicked.connect(
                    lambda _=False, n=name, k=key: self._remove(n, k)
                )
            rl.addWidget(rm)
            self._manual_list.addWidget(row)

        # Если фильтр ничего не нашёл
        if q and not any_visible:
            no_match = QLabel(f"Нет доменов по запросу «{q}»", self._list_container)
            no_match.setAlignment(Qt.AlignCenter)
            no_match.setStyleSheet(
                f"color:{theme.MUTED};font-size:12px;background:transparent;border:none;"
            )
            self._manual_list.addWidget(no_match)

        self._manual_list.addStretch()
