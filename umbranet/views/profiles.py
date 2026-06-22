"""
UmbraNet - раздел «DNS-профили» (PySide6).

Структура повторяет старую версию (netdocker), но в новом дизайне:
  • Слева две группы: «Встроенные» и «Пользовательские DNS».
    Активный профиль выбирается галочкой (☑) слева от имени.
  • Справа форма со ВСЕМИ полями: имя, IPv4 осн/доп, IPv6 осн/доп,
    DoH URL, DNSCrypt sdns://. У каждого — свой результат пинга.
  • Кнопки: ➕ добавить, 📝 клонировать, ❌ удалить, 🔄 пинг профиля.
  • Автосохранение пользовательских профилей при изменении полей.
  • Встроенные профили — только для чтения.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    get_all_dns_profiles, get_engine, get_profile_by_id, make_new_user_dns_profile,
    max_user_profiles, probe_doh, probe_host, profile_builtin_id,
    sanitize_dns_profile, save_config,
)

# поля формы: (ключ, подпись, плейсхолдер)
FORM_FIELDS = [
    ("name", "Имя профиля", "Мой DNS"),
    ("ipv4_primary", "IPv4 основной", "1.1.1.1"),
    ("ipv4_secondary", "IPv4 дополнительный", "1.0.0.1"),
    ("ipv6_primary", "IPv6 основной", "2606:4700:4700::1111"),
    ("ipv6_secondary", "IPv6 дополнительный", ""),
    ("doh_url", "DoH URL", "https://.../dns-query"),
    ("dnscrypt_stamp", "DNSCrypt sdns://", "sdns://..."),
]


class _PingAllWorker(QThread):
    finished = Signal(dict)

    def __init__(self, profiles, mode):
        super().__init__()
        self.profiles = profiles
        self.mode = mode

    def run(self):
        results = {}
        for prof in self.profiles:
            p_id = prof.get("id")
            if not p_id:
                continue

            if self.mode == "doh" and prof.get("doh_url"):
                ok, ms = probe_doh(prof["doh_url"])
            elif prof.get("ipv4_primary"):
                ok, ms = probe_host(prof["ipv4_primary"])
            else:
                ok, ms = False, None

            ping_text = f"{ms} мс" if ok and ms is not None else ("OK" if ok else "FAIL")
            results[p_id] = ping_text
        self.finished.emit(results)


class ProfilesView(QWidget):
    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self.selected_id: str | None = None
        self._fields: dict[str, QLineEdit] = {}
        self._ping_labels: dict[str, QLabel] = {}
        self._field_errors: dict[str, QLabel] = {}
        self._suppress_autosave = False

        # таймер автосохранения (debounce)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._autosave)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        title = QLabel("DNS-профили")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:22px;font-weight:700;")
        outer.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self._build_left(), 0)
        body.addWidget(self._build_editor(), 1)
        outer.addLayout(body, 1)

        self.refresh()

    # ════════════════ левая колонка (списки) ════════════════
    def _build_left(self) -> QWidget:
        wrap = QWidget()
        wrap.setFixedWidth(280)
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # «Пропинговать всё»
        self._btn_ping_all = QPushButton("📊  Пропинговать все")
        self._btn_ping_all.setCursor(Qt.PointingHandCursor)
        self._btn_ping_all.setFixedHeight(38)
        self._btn_ping_all.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;font-weight:600;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};}}")
        self._btn_ping_all.clicked.connect(self._ping_all)
        lay.addWidget(self._btn_ping_all)

        # встроенные
        b_card = QFrame()
        b_card.setStyleSheet(f"QFrame{{{theme.card_qss()}}}")
        bcl = QVBoxLayout(b_card)
        bcl.setContentsMargins(12, 10, 12, 10)
        bcl.setSpacing(6)
        bt = QLabel("🧩  Встроенные")
        bt.setStyleSheet(f"color:{theme.WHITE};font-size:13px;font-weight:700;background:transparent;border:none;")
        bcl.addWidget(bt)
        self._builtin_rows = QVBoxLayout()
        self._builtin_rows.setSpacing(4)
        bcl.addLayout(self._builtin_rows)
        lay.addWidget(b_card)

        # пользовательские
        u_card = QFrame()
        u_card.setStyleSheet(f"QFrame{{{theme.card_qss()}}}")
        ucl = QVBoxLayout(u_card)
        ucl.setContentsMargins(12, 10, 12, 10)
        ucl.setSpacing(6)
        ut = QLabel("🗂  Пользовательские")
        ut.setStyleSheet(f"color:{theme.WHITE};font-size:13px;font-weight:700;background:transparent;border:none;")
        ucl.addWidget(ut)
        self._user_rows = QVBoxLayout()
        self._user_rows.setSpacing(4)
        ucl.addLayout(self._user_rows)
        lay.addWidget(u_card, 1)

        # кнопки управления
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self._btn_add = self._icon_btn("➕", theme.GREEN, self._add_profile, "Добавить")
        self._btn_clone = self._icon_btn("📝", theme.ACCENT2, self._clone_profile, "Клонировать")
        self._btn_remove = self._icon_btn("❌", theme.RED, self._remove_profile, "Удалить")
        self._btn_ping = self._icon_btn("🔄", theme.YELLOW, self._ping_selected, "Пинг профиля")
        for b in (self._btn_add, self._btn_clone, self._btn_remove, self._btn_ping):
            ctrl.addWidget(b)
        ctrl.addStretch()
        lay.addLayout(ctrl)

        return wrap

    def _icon_btn(self, emoji, color, slot, tip) -> QPushButton:
        b = QPushButton(emoji)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(40, 36)
        b.setToolTip(tip)
        b.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{color};"
            f"border:1px solid {theme.BORDER};border-radius:9px;font-size:15px;}}"
            f"QPushButton:hover{{border-color:{color};}}"
            f"QPushButton:disabled{{color:{theme.MUTED};}}")
        b.clicked.connect(slot)
        return b

    # ════════════════ редактор ════════════════
    def _build_editor(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(f"QFrame{{{theme.card_qss(18)}}}")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(14)

        self._editor_title = QLabel("⚙  Параметры профиля")
        self._editor_title.setStyleSheet(
            f"color:{theme.WHITE};font-size:17px;font-weight:700;background:transparent;border:none;")
        outer.addWidget(self._editor_title)

        self._editor_sub = QLabel("Галочка ☑ слева делает профиль активным")
        self._editor_sub.setStyleSheet(
            f"color:{theme.MUTED};font-size:12px;background:transparent;border:none;")
        outer.addWidget(self._editor_sub)

        # ── секция: имя ──
        outer.addWidget(self._section_label("Название"))
        outer.addLayout(self._field_block("name", "Имя профиля", "Мой DNS", ping=False))

        # ── секция: IPv4 / IPv6 (парами) ──
        outer.addWidget(self._section_label("IPv4-адреса"))
        r4 = QHBoxLayout(); r4.setSpacing(12)
        r4.addLayout(self._field_block("ipv4_primary", "Основной", "1.1.1.1"), 1)
        r4.addLayout(self._field_block("ipv4_secondary", "Дополнительный", "1.0.0.1"), 1)
        outer.addLayout(r4)

        outer.addWidget(self._section_label("IPv6-адреса"))
        r6 = QHBoxLayout(); r6.setSpacing(12)
        r6.addLayout(self._field_block("ipv6_primary", "Основной", "2606:4700:4700::1111"), 1)
        r6.addLayout(self._field_block("ipv6_secondary", "Дополнительный", ""), 1)
        outer.addLayout(r6)

        # ── секция: шифрованные транспорты ──
        outer.addWidget(self._section_label("Шифрованные транспорты"))
        outer.addLayout(self._field_block("doh_url", "DoH URL", "https://.../dns-query"))
        outer.addLayout(self._field_block("dnscrypt_stamp", "DNSCrypt sdns://", "sdns://...", ping=False))

        outer.addStretch()

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        outer.addWidget(self._status)

        return panel

    # ── вспомогательные строители ──
    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setStyleSheet(
            f"color:{theme.ACCENT2};font-size:11px;font-weight:700;"
            "letter-spacing:1px;background:transparent;border:none;")
        return lbl

    def _field_block(self, key: str, label: str, placeholder: str, ping: bool = True):
        """Вертикальный блок: подпись сверху, поле снизу, опц. пинг-пилюля справа."""
        block = QVBoxLayout()
        block.setSpacing(4)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        cap = QLabel(label)
        cap.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;background:transparent;border:none;")
        head.addWidget(cap)
        head.addStretch()
        if ping:
            pl = QLabel("")
            pl.setStyleSheet(self._ping_style(theme.MUTED, faded=True))
            pl.setVisible(False)
            self._ping_labels[key] = pl
            head.addWidget(pl)
        block.addLayout(head)

        inp = QLineEdit()
        inp.setPlaceholderText(placeholder)
        inp.setMinimumHeight(38)
        inp.setStyleSheet(self._field_style(error=False))
        inp.textEdited.connect(self._on_field_edited)
        inp.textEdited.connect(lambda _t, k=key: self._validate_field(k))
        self._fields[key] = inp
        block.addWidget(inp)

        # подпись об ошибке под полем (скрыта, пока всё валидно)
        err = QLabel("")
        err.setStyleSheet(f"color:{theme.RED};font-size:10px;background:transparent;border:none;")
        err.setVisible(False)
        self._field_errors[key] = err
        block.addWidget(err)
        return block

    # ── валидация полей ──
    @staticmethod
    def _field_style(error: bool) -> str:
        border = theme.RED if error else theme.BORDER
        focus = theme.RED if error else theme.ACCENT
        return (
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {border};border-radius:9px;padding:8px 10px;font-family:Consolas;}}"
            f"QLineEdit:focus{{border-color:{focus};}}"
            f"QLineEdit:disabled{{color:{theme.MUTED};}}")

    @staticmethod
    def _check_value(key: str, value: str):
        """Возвращает (ok, текст_ошибки). Пустое значение всегда валидно."""
        value = (value or "").strip()
        if not value:
            return True, ""
        if key in ("ipv4_primary", "ipv4_secondary"):
            try:
                if ipaddress.ip_address(value).version == 4:
                    return True, ""
            except Exception:
                pass
            return False, "Некорректный IPv4-адрес"
        if key in ("ipv6_primary", "ipv6_secondary"):
            try:
                if ipaddress.ip_address(value).version == 6:
                    return True, ""
            except Exception:
                pass
            return False, "Некорректный IPv6-адрес"
        if key == "doh_url":
            try:
                p = urlsplit(value)
                if p.scheme in ("http", "https") and p.netloc:
                    return True, ""
            except Exception:
                pass
            return False, "Должен быть http(s):// адрес"
        if key == "dnscrypt_stamp":
            if value.startswith("sdns://"):
                return True, ""
            return False, "Штамп должен начинаться с sdns://"
        return True, ""

    def _validate_field(self, key: str) -> bool:
        inp = self._fields.get(key)
        err = self._field_errors.get(key)
        if inp is None:
            return True
        ok, msg = self._check_value(key, inp.text())
        inp.setStyleSheet(self._field_style(error=not ok))
        if err is not None:
            err.setText("" if ok else msg)
            err.setVisible(not ok)
        return ok

    def _validate_all(self):
        for key in self._fields:
            self._validate_field(key)

    @staticmethod
    def _ping_style(color: str, faded: bool = False) -> str:
        bg = theme.INPUT_BG if faded else color
        if faded:
            return (f"color:{color};font-size:10px;font-weight:700;font-family:Consolas;"
                    "background:transparent;border:none;")
        # цветная пилюля
        return ("color:#0e0e17;font-size:10px;font-weight:700;font-family:Consolas;"
                f"background:{color};border:none;border-radius:7px;padding:2px 8px;")


    # ════════════════ строки списка ════════════════
    def _make_row(self, prof: dict, active_id: str) -> QFrame:
        pid = prof.get("id")
        is_active = pid == active_id
        is_selected = pid == self.selected_id

        row = QFrame()
        bg = theme.INPUT_BG if is_selected else "transparent"
        border = theme.ACCENT if is_selected else "transparent"
        row.setStyleSheet(f"QFrame{{background:{bg};border:1px solid {border};border-radius:8px;}}")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(6, 4, 6, 4)
        rl.setSpacing(6)

        # галочка активации
        chk = QPushButton("☑" if is_active else "☐")
        chk.setCursor(Qt.PointingHandCursor)
        chk.setFixedWidth(26)
        chk.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;font-size:15px;"
            f"color:{theme.GREEN if is_active else theme.SUBTEXT};}}")
        chk.clicked.connect(lambda _=False, p=pid: self._activate(p))
        rl.addWidget(chk)

        # имя (выбор для редактирования)
        name = QPushButton(prof.get("name", "—"))
        name.setCursor(Qt.PointingHandCursor)
        name.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;text-align:left;"
            f"color:{theme.WHITE if is_selected else theme.TEXT};font-size:13px;}}")
        name.clicked.connect(lambda _=False, p=pid: self._select(p))
        rl.addWidget(name, 1)

        # пинг строки (заполняется при «Пропинговать все»)
        plbl = QLabel(prof.get("_ping_text", ""))
        plbl.setFixedWidth(60)
        plbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:11px;font-family:Consolas;background:transparent;border:none;")
        plbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rl.addWidget(plbl)

        return row

    # ════════════════ логика ════════════════
    def _users(self) -> list:
        return self.engine.config.setdefault("user_dns_profiles", [])

    def _selected_profile(self):
        return get_profile_by_id(self.engine.config, self.selected_id)

    def _selected_is_builtin(self) -> bool:
        p = self._selected_profile()
        return bool(p and p.get("builtin"))

    def _select(self, pid: str):
        self.selected_id = pid
        self.refresh()

    def _activate(self, pid: str):
        self.engine.config["active_dns_profile"] = pid
        save_config(self.engine.config)
        self.engine.reload_config()
        self.selected_id = pid
        self.refresh()
        self._set_status("Активный профиль изменён", theme.GREEN)

    def _add_profile(self):
        users = self._users()
        if len(users) >= max_user_profiles():
            self._set_status(f"Максимум {max_user_profiles()} профилей", theme.YELLOW)
            return
        prof = make_new_user_dns_profile(users)
        users.append(prof)
        save_config(self.engine.config)
        self.engine.reload_config()
        self.selected_id = prof.get("id")
        self.refresh()
        self._set_status("Создан новый профиль", theme.GREEN)

    def _clone_profile(self):
        src = self._selected_profile()
        if not src:
            return
        users = self._users()
        if len(users) >= max_user_profiles():
            self._set_status(f"Максимум {max_user_profiles()} профилей", theme.YELLOW)
            return
        clone = make_new_user_dns_profile(users)
        clone["name"] = f"{src.get('name', 'Профиль')} (копия)"[:20]
        for k in ("ipv4_primary", "ipv4_secondary", "ipv6_primary", "ipv6_secondary", "doh_url", "dnscrypt_stamp"):
            clone[k] = src.get(k, "")
        users.append(clone)
        save_config(self.engine.config)
        self.engine.reload_config()
        self.selected_id = clone.get("id")
        self.refresh()
        self._set_status("Профиль склонирован", theme.GREEN)

    def _remove_profile(self):
        if self._selected_is_builtin() or not self.selected_id:
            return
        self.engine.config["user_dns_profiles"] = [p for p in self._users() if p.get("id") != self.selected_id]
        if self.engine.config.get("active_dns_profile") == self.selected_id:
            self.engine.config["active_dns_profile"] = profile_builtin_id()
        self.selected_id = self.engine.config.get("active_dns_profile")
        save_config(self.engine.config)
        self.engine.reload_config()
        self.refresh()
        self._set_status("Профиль удалён", theme.GREEN)

    def _on_field_edited(self, _text):
        if self._suppress_autosave:
            return
        self._save_timer.start()  # debounce

    def _autosave(self):
        if self._selected_is_builtin() or not self.selected_id:
            return
        users = self._users()
        for i, p in enumerate(users):
            if p.get("id") == self.selected_id:
                raw = dict(p)
                for key in self._fields:
                    raw[key] = self._fields[key].text()
                clean = sanitize_dns_profile(raw)
                clean["id"] = self.selected_id
                users[i] = clean
                break
        save_config(self.engine.config)
        self.engine.reload_config()
        self._set_status("Сохранено", theme.GREEN)

    # ── пинг ──
    def _ping_selected(self):
        prof = self._selected_profile()
        if not prof:
            return
        self._set_status("Проверка...", theme.YELLOW)
        checks = [
            ("ipv4_primary", prof.get("ipv4_primary"), False),
            ("ipv4_secondary", prof.get("ipv4_secondary"), False),
            ("ipv6_primary", prof.get("ipv6_primary"), False),
            ("ipv6_secondary", prof.get("ipv6_secondary"), False),
            ("doh_url", prof.get("doh_url"), True),
        ]
        any_done = False
        for key, val, is_doh in checks:
            lbl = self._ping_labels.get(key)
            if lbl is None:
                continue
            if not val:
                lbl.setVisible(False)
                continue
            any_done = True
            ok, ms = (probe_doh(val) if is_doh else probe_host(val))
            txt = f"{ms} мс" if ok and ms is not None else ("OK" if ok else "FAIL")
            lbl.setText(txt)
            lbl.setStyleSheet(self._ping_style(theme.GREEN if ok else theme.RED))
            lbl.setVisible(True)
        self._set_status("Проверка завершена" if any_done else "Нет адресов для проверки",
                         theme.GREEN if any_done else theme.YELLOW)

    def _ping_all(self):
        if getattr(self, "_ping_worker", None) and self._ping_worker.isRunning():
            return
        self._set_status("🔍  Пингую все профили в фоне...", theme.YELLOW)
        if hasattr(self, "_btn_ping_all"):
            self._btn_ping_all.setEnabled(False)
            self._btn_ping_all.setText("⏳  Идёт тест...")

        profiles = get_all_dns_profiles(self.engine.config)
        mode = self.engine.config.get("xbox_dns_mode", "udp")

        self._ping_worker = _PingAllWorker(profiles, mode)

        def on_done(results):
            self._ping_cache = getattr(self, "_ping_cache", {})
            self._ping_cache.update(results)
            self.refresh()
            self._set_status("✅  Тест скорости завершён", theme.GREEN)
            if hasattr(self, "_btn_ping_all"):
                self._btn_ping_all.setEnabled(True)
                self._btn_ping_all.setText("📊  Пропинговать все")

        self._ping_worker.finished.connect(on_done)
        self._ping_worker.start()

    def _set_status(self, text: str, color: str = None):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color or theme.SUBTEXT};font-size:12px;background:transparent;border:none;")

    # ════════════════ обновление ════════════════
    def refresh(self):
        cfg = self.engine.config
        profiles = get_all_dns_profiles(cfg)
        active_id = cfg.get("active_dns_profile", profile_builtin_id())
        if self.selected_id is None:
            self.selected_id = active_id

        # перенесём кэш пинга в профили
        ping_cache = getattr(self, "_ping_cache", {})
        for p in profiles:
            if p.get("id") in ping_cache:
                p["_ping_text"] = ping_cache[p["id"]]

        # перестроить списки
        self._clear_layout(self._builtin_rows)
        self._clear_layout(self._user_rows)
        has_user = False
        for p in profiles:
            row = self._make_row(p, active_id)
            if p.get("builtin"):
                self._builtin_rows.addWidget(row)
            else:
                self._user_rows.addWidget(row)
                has_user = True
        if not has_user:
            empty = QLabel("Пока пусто.\nНажмите ➕, чтобы создать свой профиль.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color:{theme.MUTED};font-size:11px;background:transparent;"
                f"border:1px dashed {theme.BORDER};border-radius:8px;padding:14px 8px;")
            self._user_rows.addWidget(empty)
        self._user_rows.addStretch()

        # загрузить выбранный в форму
        self._load_editor(profiles)

        # доступность кнопок
        builtin = self._selected_is_builtin()
        self._btn_remove.setEnabled(not builtin and self.selected_id is not None)

    def _clear_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()

    def _load_editor(self, profiles: list):
        prof = next((p for p in profiles if p.get("id") == self.selected_id), None)
        self._suppress_autosave = True
        if not prof:
            for f in self._fields.values():
                f.clear()
                f.setEnabled(False)
            self._editor_title.setText("⚙  Параметры профиля")
            self._editor_sub.setText("Выберите профиль слева")
        else:
            builtin = prof.get("builtin", False)
            self._editor_title.setText(
                f"{'🔒' if builtin else '⚙'}  {prof.get('name')}")
            self._editor_sub.setText(
                "Встроенный профиль — только для чтения" if builtin
                else "Изменения сохраняются автоматически")
            for key, f in self._fields.items():
                f.setText(str(prof.get(key, "")))
                f.setEnabled(not builtin)
            # сбросить пинг-метки полей
            for lbl in self._ping_labels.values():
                lbl.setText("")
                lbl.setVisible(False)
        # сбросить/пересчитать подсветку валидации под загруженный профиль
        if not prof or prof.get("builtin", False):
            self._clear_field_marks()
        else:
            self._validate_all()
        self._suppress_autosave = False

    def _clear_field_marks(self):
        for key, inp in self._fields.items():
            inp.setStyleSheet(self._field_style(error=False))
            err = self._field_errors.get(key)
            if err is not None:
                err.setText("")
                err.setVisible(False)
