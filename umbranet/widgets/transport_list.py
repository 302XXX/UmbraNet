"""
UmbraNet - плоский список выбора транспорта DNS (без сворачивания и рамок).
Предназначен для размещения внутри постоянной рамки-карточки.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from umbranet import theme
from umbranet import engine_adapter as ea

_AUTO = "auto"

_SHORT = {
    _AUTO: "Автоматически выбирает самый быстрый маршрут",
    "udp": "Быстро, но провайдер видит и может подменять запросы",
    "doh": "Внутри HTTPS (443). Лучшая защита от подмены",
    "dot": "Внутри TLS (853). Шифрует, но порт заметнее",
    "doq": "Поверх QUIC (853). Быстрее DoT. Нужен aioquic",
    "dnscrypt": "Свой sdns:// сервер. Нужен pynacl и штамп в профиле",
}

_LABELS = {_AUTO: "Авто", **ea.TRANSPORT_LABELS}
_ORDER = [_AUTO] + list(ea.TRANSPORTS)


class _AutoPickWorker(QThread):
    """Фоновый замер транспортов -> самый быстрый (для режима «Авто»)."""
    picked = Signal(object)

    def run(self):
        self.picked.emit(ea.pick_fastest_transport())


class TransportList(QWidget):
    transportChanged = Signal(str)

    def __init__(self):
        super().__init__()
        
        # Полностью прозрачный контейнер
        self.setStyleSheet("background:transparent;border:none;")
        
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._rows: dict[str, QFrame] = {}
        self._auto = ea.auto_transport_enabled()
        self._active = ea.get_transport()
        self._auto_pick_in_progress = False
        self._auto_emit_after_pick = False

        rows_holder = QWidget()
        rows_lay = QVBoxLayout(rows_holder)
        rows_lay.setContentsMargins(0, 0, 0, 0)
        rows_lay.setSpacing(6)
        
        for key in _ORDER:
            row = self._make_row(key)
            self._rows[key] = row
            rows_lay.addWidget(row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setStyleSheet(
            f"QScrollArea{{background:transparent;border:none;}}"
            f"QScrollArea > QWidget > QWidget{{background:transparent;}}"
            + theme.scrollbar_qss())
        
        scroll_widget = QWidget()
        scroll_widget.setLayout(rows_lay)
        scroll.setWidget(scroll_widget)
        rows_holder.setStyleSheet(f"background:{theme.CARD_DARK};")
        
        # Высота под 4-5 элементов одновременно
        _row_h = 55
        scroll.setFixedHeight(4 * _row_h + 3 * 6)
        lay.addWidget(scroll)

        # Таймер авто-пика
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(30000)
        self._auto_timer.timeout.connect(self._run_auto_pick)

        self._initializing = True
        self._restyle()
        if self._auto:
            self._auto_timer.start()
            QTimer.singleShot(300, self._run_auto_pick)

        QTimer.singleShot(0, self._finish_init)

    def _finish_init(self):
        self._initializing = False

    def _make_row(self, key: str) -> QFrame:
        row = QFrame()
        row.setCursor(Qt.PointingHandCursor)
        rl = QVBoxLayout(row)
        rl.setContentsMargins(10, 7, 10, 7)
        rl.setSpacing(1)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)

        dot = QLabel("●")
        dot.setObjectName("dot")
        dot.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;")
        top.addWidget(dot)

        name = QLabel(_LABELS.get(key, key))
        name.setObjectName("name")
        name.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;border:none;")
        top.addWidget(name)
        top.addStretch()

        badge = QLabel("")
        badge.setObjectName("badge")
        badge.setStyleSheet(f"color:{theme.MUTED};font-size:10px;background:transparent;border:none;")
        top.addWidget(badge)
        rl.addLayout(top)

        desc = QLabel(_SHORT.get(key, ""))
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#b2b3d6;font-size:11px;background:transparent;border:none;")
        rl.addWidget(desc)

        row.mousePressEvent = lambda _e, k=key: self._select(k)
        return row

    def _select(self, key: str):
        if key != _AUTO:
            if key == "dnscrypt":
                if not ea.dnscrypt_available():
                    return
            else:
                avail, _ = ea.transport_available(key)
                if not avail:
                    return

        if key != _AUTO and (not self._auto) and key == self._active:
            return
        if key == _AUTO and self._auto:
            return

        if key == _AUTO:
            ea.set_auto_transport(True)
            self._auto = True
            self._auto_timer.start()
            self._auto_emit_after_pick = True
            self._run_auto_pick()
            self._restyle()
            return
            
        if key == "dnscrypt" and not ea.active_has_dnscrypt_stamp():
            from umbranet.widgets.dialogs import DnsCryptResolverDialog
            dlg = DnsCryptResolverDialog(self)
            if not dlg.exec() or not dlg.result:
                self._restyle()
                return
            ok = ea.apply_dnscrypt_resolver(dlg.result["name"], dlg.result["stamp"])
            if not ok:
                self._restyle()
                return
                
        ea.set_auto_transport(False)
        self._auto = False
        self._auto_timer.stop()
        ea.set_transport(key)
        self._active = key
        self._restyle()
        self.transportChanged.emit(key)

    def _run_auto_pick(self):
        if not self._auto:
            return
        if getattr(self, "_pick_worker", None) and self._pick_worker.isRunning():
            self._auto_emit_after_pick = True
            return
        self._auto_pick_in_progress = True
        self._pick_worker = _AutoPickWorker()
        self._pick_worker.picked.connect(self._on_auto_picked)
        self._pick_worker.start()

    def _on_auto_picked(self, mode):
        if not self._auto:
            self._auto_pick_in_progress = False
            self._restyle()
            return

        old_active = self._active
        changed = False
        if mode and mode != self._active:
            ea.set_transport(mode)
            self._active = mode
            changed = True

        should_emit = self._auto_emit_after_pick or changed
        self._auto_emit_after_pick = False
        self._auto_pick_in_progress = False
        self._restyle()

        if should_emit and not getattr(self, "_initializing", True):
            self.transportChanged.emit(_AUTO)

    def refresh(self):
        self._auto = ea.auto_transport_enabled()
        self._active = ea.get_transport()
        self._auto_pick_in_progress = False
        self._auto_emit_after_pick = False
        self._restyle()

    _UNAVAIL_HINT = {
        "doq":      "нужен aioquic",
        "dnscrypt": "нужен pynacl + sdns://",
    }

    def _restyle(self):
        for key, row in self._rows.items():
            if key == _AUTO:
                is_active = self._auto
                avail     = True
            else:
                is_active = (not self._auto) and key == self._active
                if key == "dnscrypt":
                    avail = ea.dnscrypt_available()
                else:
                    avail, _  = ea.transport_available(key)

            dot   = row.findChild(QLabel, "dot")
            name  = row.findChild(QLabel, "name")
            desc  = row.findChild(QLabel, "desc")
            badge = row.findChild(QLabel, "badge")

            if is_active:
                row.setCursor(Qt.PointingHandCursor)
                row.setStyleSheet(
                    "QFrame{background:" + theme.grad(theme.CARD_TOP, theme.ROW_BG, False) + ";"
                    f"border:1px solid {theme.ACCENT};border-radius:10px;}}"
                    "QLabel{background:transparent;border:none;}"
                )
                if dot:
                    dot.setStyleSheet(f"color:{theme.GREEN};font-size:11px;background:transparent;border:none;")
                if name:
                    name.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;border:none;")
                if desc:
                    desc.setStyleSheet("color:#b2b3d6;font-size:11px;background:transparent;border:none;")
                if badge:
                    if key == _AUTO:
                        if self._auto_pick_in_progress:
                            badge.setText("замер...")
                        else:
                            badge.setText(f"сейчас: {ea.TRANSPORT_LABELS.get(self._active, self._active)}")
                        badge.setStyleSheet(f"color:{theme.ACCENT3};font-size:10px;background:transparent;border:none;")
                    else:
                        badge.setText("")

            elif not avail:
                row.setCursor(Qt.ForbiddenCursor)
                row.setStyleSheet(
                    f"QFrame{{background:{theme.CARD_DARK};"
                    f"border:1px solid {theme.BORDER};border-radius:10px;opacity:0.6;}}"
                    "QLabel{background:transparent;border:none;}"
                )
                if dot:
                    dot.setStyleSheet(f"color:{theme.BORDER};font-size:11px;background:transparent;border:none;")
                if name:
                    name.setStyleSheet(f"color:{theme.MUTED};font-size:12px;font-weight:700;background:transparent;border:none;")
                if desc:
                    desc.setStyleSheet("color:#7c7d9c;font-size:11px;background:transparent;border:none;")
                if badge:
                    hint = self._UNAVAIL_HINT.get(key, "не установлен")
                    badge.setText(f"⚙ {hint}")
                    badge.setStyleSheet(f"color:{theme.MUTED};font-size:10px;background:transparent;border:none;")

            else:
                row.setCursor(Qt.PointingHandCursor)
                row.setStyleSheet(
                    f"QFrame{{background:{theme.ROW_BG};"
                    f"border:1px solid {theme.BORDER};border-radius:10px;}}"
                    f"QFrame:hover{{border-color:{theme.ACCENT};}}"
                    "QLabel{background:transparent;border:none;}"
                )
                if dot:
                    dot.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;")
                if name:
                    name.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;border:none;")
                if desc:
                    desc.setStyleSheet("color:#b2b3d6;font-size:11px;background:transparent;border:none;")
                if badge:
                    if key == _AUTO and self._auto:
                        if self._auto_pick_in_progress:
                            badge.setText("замер...")
                        else:
                            badge.setText(f"сейчас: {ea.TRANSPORT_LABELS.get(self._active, self._active)}")
                        badge.setStyleSheet(f"color:{theme.ACCENT3};font-size:10px;background:transparent;border:none;")
                    elif key == "dnscrypt" and not ea.active_has_dnscrypt_stamp():
                        badge.setText("выбрать sdns://")
                        badge.setStyleSheet(f"color:{theme.ACCENT3};font-size:10px;background:transparent;border:none;")
                    else:
                        badge.setText("")

    def _show_help(self):
        from umbranet.widgets.dialogs import TransportHelpDialog
        TransportHelpDialog(self).exec()

    def stop_workers(self):
        try:
            self._auto_timer.stop()
        except Exception:
            pass
        w = getattr(self, "_pick_worker", None)
        if w is not None and w.isRunning():
            try:
                w.wait(2000)
            except Exception:
                pass
