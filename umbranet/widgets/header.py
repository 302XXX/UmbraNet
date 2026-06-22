"""
UmbraNet - верхняя панель контента (PySide6).

Содержит:
  • ModeSwitch — три режима DPI (DNS Only / Combo / DPI Only);
  • ControlBar  — индикатор статуса + Start/Stop/Restart.

Сигналы:
  ModeSwitch.modeChanged(ui_key)
  ControlBar.startClicked / stopClicked / restartClicked
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from umbranet import theme

# Описания транспортов — используются TransportHelpDialog в dialogs.py
_TRANSPORT_HELP = {
    "udp": ("UDP DNS",
            "Обычный DNS по UDP/53. Самый быстрый, но провайдер видит запросы "
            "открытым текстом и может их подменять. Подходит, если блокировок "
            "по DNS нет."),
    "doh": ("DoH — DNS over HTTPS",
            "DNS-запросы прячутся внутрь обычного HTTPS (порт 443). Провайдер "
            "видит только «соединение с сайтом», не сами запросы. Лучший выбор "
            "против DNS-подмены. Используется по умолчанию."),
    "dot": ("DoT — DNS over TLS",
            "DNS внутри TLS (порт 853). Шифрует запросы, но порт 853 заметен и "
            "его иногда режут. Альтернатива DoH."),
    "doq": ("DoQ — DNS over QUIC",
            "DNS поверх QUIC (UDP/853). Быстрее DoT за счёт QUIC, шифрование как "
            "у DoH/DoT. Требует пакет aioquic."),
    "dnscrypt": ("DNSCrypt",
                 "Шифрованный протокол со штампом sdns://. Прячет и проверяет "
                 "подлинность ответов. Требует пакет pynacl и sdns-штамп в "
                 "активном профиле."),
}

# Высота кнопки power и половина — радиус для пиллюли
_BTN_H  = 40
_BTN_R  = _BTN_H // 2   # 20px — настоящая пиллюля, углов не видно


def _power_qss(bg1: str, bg2: str,
               hover1: str, hover2: str,
               pressed1: str, pressed2: str) -> str:
    """
    QSS для кнопки btn_power.

    Почему border-radius = половина высоты:
      При border-radius < 50% высоты кнопки углы видны как скруглённые прямоугольники
      (именно это и выглядело «странно»). При radius = height/2 получается
      настоящая капсула/пиллюля без видимых углов.

    Почему НЕТ QGraphicsDropShadowEffect (glow):
      Qt рисует glow по прямоугольному bounding box виджета, игнорируя border-radius
      из QSS. Итог — квадратные светящиеся углы поверх скруглённой кнопки.
      Вместо glow используем подсветку через border в :hover/:pressed.
    """
    r = f"{_BTN_R}px"
    return (
        f"QPushButton{{"
        f"  background: {theme.grad(bg1, bg2)};"
        f"  color: {theme.WHITE};"
        "  border: none;"
        f"  border-radius: {r};"
        "  font-size: 13px;"
        "  font-weight: 600;"
        "  padding: 0 20px;"
        "}}"
        f"QPushButton:hover{{"
        f"  background: {theme.grad(hover1, hover2)};"
        "  border: none;"
        f"  border-radius: {r};"
        "}}"
        f"QPushButton:pressed{{"
        f"  background: {theme.grad(pressed1, pressed2)};"
        "  border: none;"
        f"  border-radius: {r};"
        "}}"
        f"QPushButton:disabled{{"
        f"  background: {theme.CARD};"
        f"  color: {theme.MUTED};"
        f"  border: 1px solid {theme.BORDER};"
        f"  border-radius: {r};"
        "}}"
    )


def _idle_qss() -> str:
    """QSS для btn_power в состоянии «занято» (set_busy)."""
    r = f"{_BTN_R}px"
    return (
        f"QPushButton{{"
        f"  background: {theme.CARD};"
        f"  color: {theme.MUTED};"
        f"  border: 1px solid {theme.BORDER};"
        f"  border-radius: {r};"
        "  font-size: 13px;"
        "  font-weight: 600;"
        "  padding: 0 20px;"
        "}}"
    )


class ModeSwitch(QFrame):
    modeChanged = Signal(str)   # ui_key: blue/black/red

    def __init__(self, active: str = "blue"):
        super().__init__()
        self.active = active
        self._buttons: dict[str, QPushButton] = {}

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        for key, m in theme.MODES.items():
            btn = QPushButton(f"{m['emoji']}  {m['name']}")
            # Не используем Qt tooltip на кнопках режима: при быстром клике
            # DNS Only ↔ Combo Qt успевает показать маленькое всплывающее окно,
            # которое выглядит как баг/мигание. Описание режимов оставляем в
            # документации/«О программе», а сами кнопки должны переключаться
            # без всплывающих окон.
            btn.setToolTip("")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setFixedHeight(_BTN_H)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            self._buttons[key] = btn
            lay.addWidget(btn)

        self._restyle()

    def set_active(self, key: str):
        if key in self._buttons:
            self.active = key
            self._restyle()

    def _select(self, key: str):
        if key == self.active:
            self._buttons[key].setChecked(True)
            return
        self.active = key
        self._restyle()
        self.modeChanged.emit(key)

    def _restyle(self):
        r = "12px"   # ModeSwitch кнопки — скруглённые прямоугольники, не пиллюли
        for key, btn in self._buttons.items():
            m = theme.MODES[key]
            is_active = key == self.active
            btn.setChecked(is_active)
            # Всегда сбрасываем glow перед применением стиля
            btn.setGraphicsEffect(None)
            if is_active:
                btn.setStyleSheet(
                    f"QPushButton{{"
                    f"  background:{theme.grad(m['c1'], m['c2'])};"
                    f"  color:{theme.WHITE};border:none;border-radius:{r};"
                    "  font-size:13px;font-weight:600;padding:0 12px;"
                    "}}"
                    f"QPushButton:hover{{border-radius:{r};}}"
                    f"QPushButton:pressed{{border-radius:{r};}}"
                )
                theme.glow(btn, m["c1"], blur=16, dy=4)
            else:
                btn.setStyleSheet(
                    f"QPushButton{{"
                    f"  background:{theme.CARD};color:{theme.SUBTEXT};"
                    f"  border:1px solid {theme.BORDER};border-radius:{r};"
                    "  font-size:13px;padding:0 12px;"
                    "}}"
                    f"QPushButton:hover{{"
                    f"  color:{theme.TEXT};border-color:{m['c1']};border-radius:{r};"
                    "}}"
                    f"QPushButton:pressed{{border-radius:{r};}}"
                )


class ControlBar(QFrame):
    startClicked   = Signal()
    stopClicked    = Signal()
    restartClicked = Signal()

    def __init__(self, running: bool = False, mode: str = "dns_only"):
        super().__init__()
        self._running = running

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # индикатор статуса
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color:{theme.MUTED}; font-size:14px;")
        self._status = QLabel("Остановлен")
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:13px;")
        lay.addWidget(self._dot)
        lay.addWidget(self._status)
        lay.addStretch()

        # кнопка Перезапуск
        r_r = "12px"
        self.btn_restart = QPushButton("↻  Перезапуск")
        self.btn_restart.setCursor(Qt.PointingHandCursor)
        self.btn_restart.setFixedHeight(_BTN_H)
        self.btn_restart.setStyleSheet(
            f"QPushButton{{"
            f"  background:{theme.grad(theme.ORANGE,'#f59e0b')};color:{theme.WHITE};"
            f"  border:none;border-radius:{r_r};"
            "  font-size:13px;font-weight:600;padding:0 12px;"
            "}}"
            f"QPushButton:hover{{border-radius:{r_r};}}"
            f"QPushButton:pressed{{border-radius:{r_r};}}"
            f"QPushButton:disabled{{"
            f"  background:{theme.CARD};color:{theme.MUTED};"
            f"  border:1px solid {theme.BORDER};border-radius:{r_r};"
            "}}"
        )
        self.btn_restart.clicked.connect(self.restartClicked.emit)
        lay.addWidget(self.btn_restart)

        # кнопка Старт / Стоп (пиллюля)
        self.btn_power = QPushButton()
        self.btn_power.setCursor(Qt.PointingHandCursor)
        self.btn_power.setFixedHeight(_BTN_H)
        self.btn_power.setMinimumWidth(140)
        self.btn_power.clicked.connect(self._on_power)
        lay.addWidget(self.btn_power)

        self.set_running(running, mode=mode)

    def _on_power(self):
        if self._running:
            self.stopClicked.emit()
        else:
            self.startClicked.emit()

    # ── публичные методы ──────────────────────────────────────────────────────

    def set_running(self, running: bool, mode: str = "dns_only", admin_warn: bool = False):
        self._running = running
        # Убираем любой graphics-эффект — glow не совместим с border-radius QSS
        self.btn_power.setGraphicsEffect(None)

        if running:
            color = theme.YELLOW if admin_warn else theme.GREEN
            self._dot.setStyleSheet(f"color:{color}; font-size:14px;")

            # Определяем текст статуса в зависимости от режима
            if mode == "dns_only":
                status_text = "DNS запущен"
            elif mode == "combo":
                status_text = "DNS + DPI запущены"
            elif mode == "dpi_only":
                status_text = "DPI запущено"
            else:
                status_text = "Работает"

            self._status.setText(
                "Нужны права администратора" if admin_warn else status_text
            )
            self._status.setStyleSheet(f"color:{color}; font-size:13px;")
            self.btn_power.setText("⏹  Стоп")
            self.btn_power.setStyleSheet(_power_qss(
                bg1=theme.RED,      bg2="#f43f5e",
                hover1="#ff6070",   hover2="#ff3050",
                pressed1="#cc0020", pressed2="#cc2040",
            ))
            self.btn_restart.setEnabled(True)
            self.btn_power.setEnabled(True)
        else:
            self._dot.setStyleSheet(f"color:{theme.MUTED}; font-size:14px;")
            self._status.setText("Остановлен")
            self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:13px;")
            self.btn_power.setText("▶  Старт")
            self.btn_power.setStyleSheet(_power_qss(
                bg1=theme.GREEN,    bg2="#10b981",
                hover1="#55ffaa",   hover2="#20d490",
                pressed1="#1e8a55", pressed2="#0e7a45",
            ))
            self.btn_restart.setEnabled(False)
            self.btn_power.setEnabled(True)

    def set_busy(self, action: str):
        """Промежуточный статус пока идёт start/stop/restart."""
        self._running = False
        self.btn_power.setGraphicsEffect(None)
        self.btn_power.setEnabled(False)
        self.btn_restart.setEnabled(False)
        labels = {
            "start":   ("▶  Запуск...",     theme.GREEN,  "Запуск..."),
            "stop":    ("⏹  Остановка...",  theme.YELLOW, "Остановка..."),
            "restart": ("↻  Перезапуск...", theme.ORANGE, "Перезапуск..."),
        }
        btn_text, color, status_text = labels.get(action, ("...", theme.MUTED, "..."))
        self._dot.setStyleSheet(f"color:{color}; font-size:14px;")
        self._status.setText(status_text)
        self._status.setStyleSheet(f"color:{color}; font-size:13px;")
        self.btn_power.setText(btn_text)
        self.btn_power.setStyleSheet(_idle_qss())

    def set_ai_busy(self):
        """Промежуточный статус controlled AI-генерации."""
        self._running = False
        self.btn_power.setGraphicsEffect(None)
        self.btn_power.setEnabled(False)
        self.btn_restart.setEnabled(False)
        self._dot.setStyleSheet(f"color:{theme.ACCENT3}; font-size:14px;")
        self._status.setText("AI-генерация...")
        self._status.setStyleSheet(f"color:{theme.ACCENT3}; font-size:13px;")
        self.btn_power.setText("🧪  Генерация...")
        self.btn_power.setStyleSheet(_idle_qss())

    def set_error(self, message: str):
        """Показывает ошибку запуска."""
        self.btn_power.setGraphicsEffect(None)
        self._dot.setStyleSheet(f"color:{theme.RED}; font-size:14px;")
        short = (message[:45] + "…") if len(message) > 45 else message
        self._status.setText(f"Ошибка: {short}")
        self._status.setStyleSheet(f"color:{theme.RED}; font-size:12px;")
        self.btn_power.setEnabled(True)
        self.btn_restart.setEnabled(False)
