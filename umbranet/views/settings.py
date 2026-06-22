"""
UmbraNet - раздел «Настройки» (PySide6).

Перенос окна настроек из старой версии (3 секции), с автосохранением:
  • DNS-сервер: порт, fallback IPv4/IPv6;
  • Поведение: route_all, IPv6-сервер, автозапуск, стратегия upstream;
  • Routed-кэш: пресет режима + кэш/TTL (слайдеры) + optimistic cache.

Всё сохраняется СРАЗУ при изменении (как в DNS-профилях). Пресет режима
показывает только реальные пресеты; «Пользовательский» появляется в подписи
сам, когда значения не совпадают ни с одним пресетом.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    autostart_enabled, autostart_set, autostart_supported, backup_create,
    backup_list, backup_load, get_active_dns_profile, get_engine,
    get_routed_preset_map, save_config, upstream_modes,
    parse_domain_lines, set_filter_lists,
)
from umbranet.widgets.slider_field import SliderField
from umbranet.widgets.toggle import Toggle

# Стратегии upstream: ключ ядра -> русская подпись
UPSTREAM_LABELS = {
    "parallel": "Параллельно (быстрее)",
    "fastest": "Самый быстрый",
    "sequential": "По очереди",
}

# Поля пресета кэша (по ним определяется «Пользовательский»)
PRESET_FIELDS = ("routed_cache_enabled", "routed_cache_ttl", "routed_reply_ttl",
                 "optimistic_cache_enabled", "stale_cache_ttl")


def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setStyleSheet(f"QFrame{{{theme.card_qss(14)}}}")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 12, 16, 14)
    lay.setSpacing(10)
    t = QLabel(title)
    t.setStyleSheet(f"color:{theme.WHITE};font-size:14px;font-weight:700;background:transparent;border:none;")
    lay.addWidget(t)
    return f, lay


class _NoWheelComboBox(QComboBox):
    """QComboBox без случайного переключения колесом мыши.

    Когда пользователь прокручивает страницу настроек, курсор часто оказывается
    над combo-box. Стандартный QComboBox меняет выбранный пункт от wheelEvent
    даже без клика. Для настроек это опасно, поэтому колесо игнорируем.
    Открытый выпадающий список при этом продолжает работать штатно.
    """

    def wheelEvent(self, event):
        event.ignore()



class SettingsView(QWidget):
    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self._suppress = False  # подавление автосейва при программном обновлении
        cfg = self.engine.config

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(12)

        # заголовок + статус + кнопки
        head = QHBoxLayout()
        title = QLabel("Настройки")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:22px;font-weight:700;")
        head.addWidget(title)
        head.addStretch()
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{theme.GREEN};font-size:12px;")
        head.addWidget(self._status)
        backup_btn = self._small_btn("⤓ Резервная копия", theme.ACCENT2)
        backup_btn.clicked.connect(self._backup)
        restore_btn = self._small_btn("↺ Восстановить", theme.ACCENT)
        restore_btn.clicked.connect(self._restore)
        reset_btn = self._small_btn("⟳ Сбросить", theme.BORDER, theme.TEXT)
        reset_btn.clicked.connect(self._reset_defaults)
        head.addWidget(backup_btn)
        head.addWidget(restore_btn)
        head.addWidget(reset_btn)
        outer.addLayout(head)

        body = QVBoxLayout()
        body.setSpacing(12)

        # ── Секция 0: Темы ──
        stheme, ltheme = _section("🎨  Темы")
        theme_desc = QLabel(
            "Выберите внешний вид UmbraNet. Тема сохраняется сразу и полностью "
            "применится после перезапуска приложения."
        )
        theme_desc.setWordWrap(True)
        theme_desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        ltheme.addWidget(theme_desc)

        theme_row = QHBoxLayout()
        theme_lbl = QLabel("Тема приложения")
        theme_lbl.setFixedWidth(150)
        theme_lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        self._theme_combo = _NoWheelComboBox()
        for key, label in theme.theme_items():
            self._theme_combo.addItem(label, key)
        ti = self._theme_combo.findData(theme.CURRENT_THEME)
        if ti >= 0:
            self._theme_combo.setCurrentIndex(ti)
        self._theme_combo.setFixedHeight(34)
        self._theme_combo.setStyleSheet(self._combo_qss())
        self._theme_combo.currentIndexChanged.connect(lambda _=0: self._on_theme_changed())
        theme_row.addWidget(theme_lbl)
        theme_row.addWidget(self._theme_combo)
        theme_row.addStretch()
        ltheme.addLayout(theme_row)
        body.addWidget(stheme)

        # ── Секция 1: DNS-сервер ──
        s1, l1 = _section("🖧  DNS-сервер")
        self._port = self._line_field(l1, "Порт", str(cfg.get("listen_port", 53)), 90)
        self._fb4 = self._line_field(l1, "Fallback IPv4", cfg.get("fallback_dns", "8.8.8.8"))
        self._fb6 = self._line_field(l1, "Fallback IPv6", cfg.get("fallback_dns6", ""), 240)
        body.addWidget(s1)

        # ── Секция 2: Поведение ──
        s2, l2 = _section("⚡  Поведение")
        prof_name = get_active_dns_profile(cfg).get("name", "профиль")
        self._route_all = self._toggle_row(l2, f"Маршрутизировать ВСЕ домены через профиль ({prof_name})",
                                           cfg.get("route_all", False))
        self._ipv6 = self._toggle_row(l2, "Включить IPv6 DNS-сервер", cfg.get("enable_ipv6", True))
        self._ipv6_priority = self._toggle_row(l2, "Приоритет IPv6 для заблокированных сайтов (трюк обхода)",
                                               cfg.get("ipv6_priority_enabled", False))

        self._autostart = None
        if autostart_supported():
            self._autostart = self._toggle_row(l2, "Запускать UmbraNet при включении Windows",
                                               autostart_enabled())
        else:
            note = QLabel("Автозапуск доступен только на Windows")
            note.setStyleSheet(f"color:{theme.MUTED};font-size:12px;background:transparent;border:none;")
            l2.addWidget(note)

        # стратегия upstream
        up_row = QHBoxLayout()
        up_lbl = QLabel("Стратегия upstream")
        up_lbl.setFixedWidth(150)
        up_lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        self._upstream = _NoWheelComboBox()
        for mode in upstream_modes():
            self._upstream.addItem(UPSTREAM_LABELS.get(mode, mode), mode)
        i = self._upstream.findData(cfg.get("upstream_mode", "parallel"))
        if i >= 0:
            self._upstream.setCurrentIndex(i)
        self._upstream.setFixedHeight(34)
        self._upstream.setStyleSheet(self._combo_qss())
        self._upstream.currentIndexChanged.connect(lambda _=0: self._autosave())
        up_row.addWidget(up_lbl)
        up_row.addWidget(self._upstream)
        up_row.addStretch()
        l2.addLayout(up_row)
        body.addWidget(s2)

        # ── Секция 3: Routed-кэш ──
        s3, l3 = _section("🗃  Routed-домены: кэш и TTL")
        preset_row = QHBoxLayout()
        p_lbl = QLabel("Пресет режима")
        p_lbl.setFixedWidth(150)
        p_lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        self._preset = _NoWheelComboBox()
        self._presets_map = get_routed_preset_map()
        # ТОЛЬКО реальные пресеты (без «Пользовательский» — он авто-подпись)
        self._preset.addItems(list(self._presets_map.keys()))
        self._preset.setFixedHeight(34)
        self._preset.setStyleSheet(self._combo_qss())
        self._preset.activated.connect(self._on_preset_chosen)  # только по клику юзера
        preset_row.addWidget(p_lbl)
        preset_row.addWidget(self._preset)
        preset_row.addStretch()
        l3.addLayout(preset_row)

        self._cache_on = self._toggle_row(l3, "Внутренний кэш для routed-доменов",
                                          cfg.get("routed_cache_enabled", True))
        # слайдеры TTL
        self._cache_ttl = SliderField("TTL кэша", int(cfg.get("routed_cache_ttl", 5)), 0, 120)
        self._cache_ttl.valueChanged.connect(lambda _=0: self._autosave())
        l3.addWidget(self._cache_ttl)
        self._reply_ttl = SliderField("TTL ответа", int(cfg.get("routed_reply_ttl", 1)), 0, 60)
        self._reply_ttl.valueChanged.connect(lambda _=0: self._autosave())
        l3.addWidget(self._reply_ttl)
        self._optim = self._toggle_row(l3, "Optimistic cache (мгновенные ответы из «просроченного» кэша)",
                                       cfg.get("optimistic_cache_enabled", True))
        self._stale_ttl = SliderField("Stale TTL", int(cfg.get("stale_cache_ttl", 3600)), 0, 86400)
        self._stale_ttl.valueChanged.connect(lambda _=0: self._autosave())
        l3.addWidget(self._stale_ttl)
        body.addWidget(s3)

        # ── Секция 4: DNS-фильтрация ──
        sf, lf = _section("🚦  DNS-фильтрация: blocklist / allowlist")
        desc = QLabel(
            "Можно вставлять обычные домены, hosts-формат (0.0.0.0 domain) "
            "и простые AdBlock-правила вида ||domain^. Allowlist имеет приоритет над blocklist."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lf.addWidget(desc)

        lists_row = QHBoxLayout()
        self._blocked_edit = self._domain_list_editor(
            "Блокировать (NXDOMAIN)", cfg.get("blocked_domains", [])
        )
        self._allow_edit = self._domain_list_editor(
            "Разрешать всегда (allowlist)", cfg.get("allowlist_domains", [])
        )
        lists_row.addWidget(self._blocked_edit["wrap"], 1)
        lists_row.addWidget(self._allow_edit["wrap"], 1)
        lf.addLayout(lists_row)

        filter_row = QHBoxLayout()
        self._filter_status = QLabel(self._filter_status_text())
        self._filter_status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        filter_row.addWidget(self._filter_status, 1)
        self._filter_save_btn = self._small_btn("✓ Сохранить списки", theme.GREEN)
        self._filter_save_btn.clicked.connect(self._save_filter_lists)
        filter_row.addWidget(self._filter_save_btn)
        lf.addLayout(filter_row)
        body.addWidget(sf)

        body.addStretch()

        # прокрутка со стилизованным скроллбаром
        bodyw = QWidget()
        bodyw.setLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())
        scroll.setWidget(bodyw)
        outer.addWidget(scroll, 1)

        self._update_preset_label()

    # ── фабрики строк (с автосейвом) ──
    def _line_field(self, parent_lay, label, value, width=160) -> QLineEdit:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(150)
        lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        inp = QLineEdit(value)
        inp.setFixedWidth(width)
        inp.setFixedHeight(34)
        inp.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;font-family:Consolas;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}")
        inp.editingFinished.connect(self._autosave)  # сохранить при потере фокуса/Enter
        row.addWidget(lbl)
        row.addWidget(inp)
        row.addStretch()
        parent_lay.addLayout(row)
        return inp

    def _domain_list_editor(self, title: str, values: list) -> dict:
        wrap = QFrame()
        wrap.setStyleSheet("QFrame{background:transparent;border:none;}")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color:{theme.TEXT};font-size:13px;font-weight:700;background:transparent;border:none;")
        lay.addWidget(lbl)
        edit = QPlainTextEdit()
        edit.setPlainText("\n".join(values or []))
        edit.setMinimumHeight(130)
        edit.setPlaceholderText("example.com\n0.0.0.0 ads.example.com\n||tracker.example.net^")
        edit.setStyleSheet(
            f"QPlainTextEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:8px;"
            "font-family:Consolas;font-size:12px;}}" + theme.scrollbar_qss()
        )
        lay.addWidget(edit)
        return {"wrap": wrap, "edit": edit}

    def _toggle_row(self, parent_lay, label, checked) -> Toggle:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{theme.TEXT};font-size:13px;background:transparent;border:none;")
        tg = Toggle(checked)
        tg.toggled.connect(lambda _=False: self._autosave())
        row.addWidget(lbl, 1)
        row.addWidget(tg)
        parent_lay.addLayout(row)
        return tg

    # ── стили ──
    def _combo_qss(self) -> str:
        return (
            f"QComboBox{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:8px;padding:0 10px;min-width:200px;}}"
            f"QComboBox:hover{{border-color:{theme.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{theme.CARD};color:{theme.TEXT};"
            f"selection-background-color:{theme.ACCENT};border:1px solid {theme.BORDER};}}")

    def _small_btn(self, text, bg, fg=None) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        b.setStyleSheet(
            f"QPushButton{{background:{bg};color:{fg or theme.WHITE};"
            "border:none;border-radius:9px;padding:0 14px;font-size:12px;}}"
            f"QPushButton:hover{{background:{theme.ACCENT};color:{theme.WHITE};}}")
        return b

    # ── логика ──
    def _on_theme_changed(self):
        if self._suppress or not hasattr(self, "_theme_combo"):
            return
        name = self._theme_combo.currentData()
        saved = theme.save_theme_preference(name)
        self._set_status(f"✓ Тема «{theme.theme_label(saved)}» сохранена. Перезапустите UmbraNet", theme.ACCENT3)

    def _on_preset_chosen(self, index: int):
        name = self._preset.itemText(index)
        preset = self._presets_map.get(name)
        if not preset:
            return
        self._suppress = True
        self._cache_on.setChecked(bool(preset.get("routed_cache_enabled", True)))
        self._cache_ttl.setValue(int(preset.get("routed_cache_ttl", 5)))
        self._reply_ttl.setValue(int(preset.get("routed_reply_ttl", 1)))
        self._optim.setChecked(bool(preset.get("optimistic_cache_enabled", True)))
        self._stale_ttl.setValue(int(preset.get("stale_cache_ttl", 3600)))
        self._suppress = False
        self._autosave()

    def _current_preset_name(self) -> str | None:
        """Имя пресета, совпадающего с текущими значениями, иначе None."""
        cur = {
            "routed_cache_enabled": self._cache_on.isChecked(),
            "routed_cache_ttl": self._cache_ttl.value(),
            "routed_reply_ttl": self._reply_ttl.value(),
            "optimistic_cache_enabled": self._optim.isChecked(),
            "stale_cache_ttl": self._stale_ttl.value(),
        }
        for name, preset in self._presets_map.items():
            if all(cur[f] == preset.get(f) for f in PRESET_FIELDS):
                return name
        return None

    def _update_preset_label(self):
        name = self._current_preset_name()
        self._preset.blockSignals(True)
        if name:
            i = self._preset.findText(name)
            if i >= 0:
                self._preset.setCurrentIndex(i)
        else:
            # значения «пользовательские» — показываем это как временный заголовок
            self._preset.setCurrentIndex(-1)
            self._preset.setEditable(False)
            self._preset.setPlaceholderText("Пользовательский")
        self._preset.blockSignals(False)

    def _autosave(self):
        if self._suppress:
            return
        cfg = self.engine.config
        try:
            cfg["listen_port"] = int(self._port.text())
        except ValueError:
            self._set_status("Порт должен быть числом", theme.RED)
            return
        cfg["fallback_dns"] = self._fb4.text().strip()
        cfg["fallback_dns6"] = self._fb6.text().strip()
        cfg["route_all"] = self._route_all.isChecked()
        cfg["enable_ipv6"] = self._ipv6.isChecked()
        cfg["ipv6_priority_enabled"] = self._ipv6_priority.isChecked()
        cfg["upstream_mode"] = self._upstream.currentData()
        cfg["routed_cache_enabled"] = self._cache_on.isChecked()
        cfg["optimistic_cache_enabled"] = self._optim.isChecked()
        cfg["routed_cache_ttl"] = self._cache_ttl.value()
        cfg["routed_reply_ttl"] = self._reply_ttl.value()
        cfg["stale_cache_ttl"] = self._stale_ttl.value()

        save_config(cfg)
        self.engine.reload_config()

        if self._autostart is not None:
            try:
                autostart_set(self._autostart.isChecked())
            except Exception:
                pass

        self._update_preset_label()
        self._set_status("✓ Сохранено", theme.GREEN)
        QTimer.singleShot(1500, lambda: self._set_status("", theme.GREEN))

    def _backup(self):
        path = backup_create(self.engine.config)
        self._set_status("✓ Резервная копия создана" if path else "Не удалось создать копию",
                         theme.GREEN if path else theme.RED)

    def _restore(self):
        from umbranet.widgets.dialogs import RestoreBackupDialog
        dlg = RestoreBackupDialog(backup_list(), self)
        if not dlg.exec() or not dlg.result:
            return
        cfg = backup_load(dlg.result)
        if not cfg:
            self._set_status("Не удалось прочитать резервную копию", theme.RED)
            return
        self.engine.config.clear()
        self.engine.config.update(cfg)
        save_config(self.engine.config)
        self.engine.reload_config()
        self._load_form_from_cfg(self.engine.config)
        self._set_status("✓ Настройки восстановлены", theme.GREEN)

    def _load_form_from_cfg(self, cfg):
        self._suppress = True
        self._port.setText(str(cfg.get("listen_port", 53)))
        self._fb4.setText(cfg.get("fallback_dns", "8.8.8.8"))
        self._fb6.setText(cfg.get("fallback_dns6", ""))
        self._route_all.setChecked(bool(cfg.get("route_all", False)))
        self._ipv6.setChecked(bool(cfg.get("enable_ipv6", True)))
        i = self._upstream.findData(cfg.get("upstream_mode", "parallel"))
        if i >= 0:
            self._upstream.setCurrentIndex(i)
        self._cache_on.setChecked(bool(cfg.get("routed_cache_enabled", True)))
        self._cache_ttl.setValue(int(cfg.get("routed_cache_ttl", 5)))
        self._reply_ttl.setValue(int(cfg.get("routed_reply_ttl", 1)))
        self._optim.setChecked(bool(cfg.get("optimistic_cache_enabled", True)))
        self._stale_ttl.setValue(int(cfg.get("stale_cache_ttl", 3600)))
        if hasattr(self, "_blocked_edit"):
            self._blocked_edit["edit"].setPlainText("\n".join(cfg.get("blocked_domains", []) or []))
            self._allow_edit["edit"].setPlainText("\n".join(cfg.get("allowlist_domains", []) or []))
            self._filter_status.setText(self._filter_status_text())
        if self._autostart is not None:
            try:
                self._autostart.setChecked(autostart_enabled())
            except Exception:
                pass
        self._suppress = False
        self._update_preset_label()

    def _reset_defaults(self):
        self._suppress = True
        self._port.setText("53")
        self._fb4.setText("8.8.8.8")
        self._fb6.setText("2001:4860:4860::8888")
        self._route_all.setChecked(False)
        self._ipv6.setChecked(True)
        i = self._upstream.findData("parallel")
        if i >= 0:
            self._upstream.setCurrentIndex(i)
        self._cache_on.setChecked(True)
        self._cache_ttl.setValue(5)
        self._reply_ttl.setValue(1)
        self._optim.setChecked(True)
        self._stale_ttl.setValue(3600)
        self._suppress = False
        self._autosave()
        self._set_status("Значения сброшены", theme.YELLOW)

    def _set_status(self, text, color=None):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color or theme.GREEN};font-size:12px;")

    # ── DNS-фильтрация ──
    def _filter_status_text(self) -> str:
        cfg = self.engine.config
        return (
            f"Блоклист: {len(cfg.get('blocked_domains', []) or [])} • "
            f"Allowlist: {len(cfg.get('allowlist_domains', []) or [])}"
        )

    def _save_filter_lists(self):
        blocked = parse_domain_lines(
            self._blocked_edit["edit"].toPlainText(),
            include_adblock_exceptions=False,
        )
        allow = parse_domain_lines(self._allow_edit["edit"].toPlainText())
        if set_filter_lists(blocked=blocked, allowlist=allow):
            # normalize visible text after parsing hosts/adblock syntax
            self._blocked_edit["edit"].setPlainText("\n".join(blocked))
            self._allow_edit["edit"].setPlainText("\n".join(allow))
            self.engine.config["blocked_domains"] = blocked
            self.engine.config["allowlist_domains"] = allow
            self._filter_status.setText(self._filter_status_text())
            self._set_status("✓ DNS-фильтры сохранены", theme.GREEN)
        else:
            self._set_status("Не удалось сохранить DNS-фильтры", theme.RED)

    def refresh(self):
        """Обновляет статусы настроек при переключении на вкладку."""
        if hasattr(self, "_filter_status"):
            self._filter_status.setText(self._filter_status_text())
