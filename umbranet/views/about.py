"""
UmbraNet - раздел «О программе».

Короткая справка для пользователя + техническая информация, которую удобно
скопировать при отладке.
"""

from __future__ import annotations

import platform
import sys
from importlib import metadata

from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QSizePolicy,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from umbranet import __version__, theme
from umbranet.widgets.rounded_panel import RoundedPanel
from umbranet import engine_adapter as ea

_PACKAGE_NAMES = {
    "PySide6": "PySide6",
    "dnslib": "dnslib",
    "requests": "requests",
    "psutil": "psutil",
    "aioquic": "aioquic",
    "pynacl": "PyNaCl",
    "pydivert": "pydivert",
}


def _pkg_version(dist_name: str) -> str:
    try:
        return metadata.version(dist_name)
    except Exception:
        return "не установлен"


def _card(title: str = "") -> tuple[QWidget, QVBoxLayout]:
    f = RoundedPanel(theme.CARD, theme.BORDER, radius=14)
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(10)
    if title:
        t = QLabel(title)
        t.setStyleSheet(
            f"color:{theme.WHITE};font-size:15px;font-weight:700;"
            "background:transparent;border:none;"
        )
        lay.addWidget(t)
    return f, lay


class AboutView(QWidget):
    def __init__(self):
        super().__init__()
        self.engine = ea.get_engine()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("О программе")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:22px;font-weight:700;")
        head.addWidget(title)
        head.addStretch()
        self._copy_btn = self._small_btn("📋 Скопировать отчёт", theme.ACCENT)
        self._copy_btn.clicked.connect(self._copy_report)
        head.addWidget(self._copy_btn)
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())

        body = QWidget()
        try:
            body.setAttribute(Qt.WA_StaticContents, True)
        except Exception:
            pass
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        lay.addWidget(self._build_hero())
        lay.addWidget(self._build_status())
        lay.addWidget(self._build_help())
        lay.addWidget(self._build_tech())
        lay.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    # ── UI blocks ────────────────────────────────────────────────────────────
    def _build_hero(self) -> QFrame:
        card, lay = _card("")

        top = QHBoxLayout()
        logo = QLabel("Umbra<span style='color:%s;'>Net</span>" % theme.ACCENT2)
        logo.setTextFormat(Qt.RichText)
        logo.setStyleSheet(
            f"color:{theme.WHITE};font-size:32px;font-weight:800;"
            "background:transparent;border:none;"
        )
        top.addWidget(logo)
        top.addStretch()
        ver = QLabel(f"v{__version__}")
        ver.setStyleSheet(
            f"color:{theme.ACCENT3};font-size:13px;font-weight:700;"
            f"background:{theme.INPUT_BG};border:1px solid {theme.BORDER};"
            "border-radius:10px;padding:5px 10px;"
        )
        top.addWidget(ver)
        lay.addLayout(top)

        desc = QLabel(
            "Локальный DNS-инструмент для Windows: выборочная маршрутизация доменов, "
            "защита от DNS-подмены провайдера, DoH/DoT/DoQ/DNSCrypt-транспорты, "
            "журнал запросов и диагностика проблем доступа."
        )
        desc.setWordWrap(True)
        desc.setFixedHeight(40)
        try:
            desc.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        except Exception:
            pass
        desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        lay.addWidget(desc)

        privacy = QLabel("🔒 Все настройки и журналы хранятся локально в папке программы.")
        privacy.setWordWrap(True)
        privacy.setFixedHeight(18)
        try:
            privacy.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        except Exception:
            pass
        privacy.setStyleSheet(f"color:{theme.ACCENT3};font-size:12px;background:transparent;border:none;")
        lay.addWidget(privacy)
        return card

    def _build_status(self) -> QFrame:
        card, lay = _card("🧩  Состояние компонентов")
        self._status_grid = QGridLayout()
        self._status_grid.setHorizontalSpacing(14)
        self._status_grid.setVerticalSpacing(8)
        lay.addLayout(self._status_grid)
        self._fill_status_grid()
        return card

    def _build_help(self) -> QFrame:
        card, lay = _card("💡  Где что находится")
        # Один QLabel вместо 5 — в 4× меньше heightForWidth пересчётов при ресайзе
        rows = [
            "<b>Маршрутизация</b> — Включайте сервисы и домены, которые должны идти через обход.",
            "<b>Сеть и диагностика</b> — Проверяйте DNS, утечки, доступность сервисов и причину, почему сайт не открывается.",
            "<b>DNS-профили</b> — Настраивайте провайдеров и защищённые транспорты: DoH, DoT, DoQ, DNSCrypt.",
            "<b>Логи</b> — Смотрите живые DNS-запросы и системные логи, добавляйте домены в обход, blocklist или allowlist.",
            "<b>Настройки</b> — Порт DNS, IPv6, кэш, bogus-IP и ручная DNS-фильтрация.",
        ]
        txt = "<br>".join(f"<div style='margin:4px 0;'>{r}</div>" for r in rows)
        lab = QLabel(txt)
        lab.setTextFormat(Qt.RichText)
        lab.setWordWrap(True)
        # Фикс — убирает повторные heightForWidth на каждый пиксель
        lab.setFixedHeight(110)
        try:
            lab.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        except Exception:
            pass
        lab.setStyleSheet(f"color:{theme.TEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(lab)
        return card

    def _build_tech(self) -> QFrame:
        card, lay = _card("🛠  Техническая информация")
        self._tech_grid = QGridLayout()
        self._tech_grid.setHorizontalSpacing(14)
        self._tech_grid.setVerticalSpacing(8)
        lay.addLayout(self._tech_grid)
        self._fill_tech_grid()
        return card

    # ── Fillers ──────────────────────────────────────────────────────────────
    def _fill_status_grid(self):
        health = ea.get_startup_health()
        rows = [
            ("DNS-ядро", "настоящее" if ea.is_real_engine() else "заглушка", ea.is_real_engine()),
            ("Права администратора", "есть" if ea.is_admin() else "нет", ea.is_admin()),
            ("DNS-сервер", "работает" if self.engine.running else "остановлен", bool(self.engine.running)),
            ("DoQ", "доступен" if ea.doq_available() else "нужен aioquic", ea.doq_available()),
            ("DNSCrypt", "доступен" if ea.dnscrypt_available() else "нужен pynacl", ea.dnscrypt_available()),
            ("Предстартовая проверка", health.get("summary", "—"), health.get("severity") != "error"),
        ]
        for r, (name, value, ok) in enumerate(rows):
            self._kv(self._status_grid, r, name, value, ok=ok)

    def _fill_tech_grid(self):
        rows = [
            ("Версия UmbraNet", __version__),
            ("Python", sys.version.split()[0]),
            ("Qt", qVersion()),
            ("ОС", f"{platform.system()} {platform.release()}".strip()),
            ("PySide6", _pkg_version(_PACKAGE_NAMES["PySide6"])),
            ("dnslib", _pkg_version(_PACKAGE_NAMES["dnslib"])),
            ("requests", _pkg_version(_PACKAGE_NAMES["requests"])),
            ("psutil", _pkg_version(_PACKAGE_NAMES["psutil"])),
            ("aioquic", _pkg_version(_PACKAGE_NAMES["aioquic"])),
            ("PyNaCl", _pkg_version(_PACKAGE_NAMES["pynacl"])),
            ("pydivert", _pkg_version(_PACKAGE_NAMES["pydivert"])),
        ]
        for r, (name, value) in enumerate(rows):
            self._kv(self._tech_grid, r, name, value)

    def _kv(self, grid: QGridLayout, row: int, key: str, value: str, ok: bool | None = None):
        k = QLabel(key)
        k.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        v = QLabel(str(value))
        # Без wordWrap — фиксированная высота, нет heightForWidth при ресайзе
        v.setWordWrap(False)
        color = theme.TEXT
        if ok is True:
            color = theme.GREEN
        elif ok is False:
            color = theme.YELLOW
        v.setStyleSheet(f"color:{color};font-size:12px;font-weight:600;background:transparent;border:none;")
        grid.addWidget(k, row, 0, Qt.AlignTop)
        grid.addWidget(v, row, 1, Qt.AlignTop)
        grid.setColumnStretch(1, 1)

    def _small_btn(self, text, bg, fg=None) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        b.setStyleSheet(
            f"QPushButton{{background:{bg};color:{fg or theme.WHITE};"
            "border:none;border-radius:9px;padding:0 14px;font-size:12px;font-weight:600;}}"
            f"QPushButton:hover{{background:{theme.ACCENT2};color:{theme.WHITE};}}"
        )
        return b

    # ── Report ───────────────────────────────────────────────────────────────
    def _report_text(self) -> str:
        health = ea.get_startup_health()
        lines = [
            f"UmbraNet v{__version__}",
            "=" * 48,
            f"Python: {sys.version.split()[0]}",
            f"Qt: {qVersion()}",
            f"OS: {platform.platform()}",
            f"Admin: {ea.is_admin()}",
            f"Real engine: {ea.is_real_engine()}",
            f"DNS running: {bool(self.engine.running)}",
            f"Startup health: {health.get('severity')} — {health.get('summary')}",
            "",
            "Dependencies:",
        ]
        for label, dist in _PACKAGE_NAMES.items():
            lines.append(f"  {label}: {_pkg_version(dist)}")
        return "\n".join(lines)

    def _copy_report(self):
        QGuiApplication.clipboard().setText(self._report_text())
        self._copy_btn.setText("✓ Скопировано")
        self._copy_btn.setStyleSheet(
            f"QPushButton{{background:{theme.GREEN};color:{theme.WHITE};"
            "border:none;border-radius:9px;padding:0 14px;font-size:12px;font-weight:600;}}"
        )

    def refresh(self):
        self.engine = ea.get_engine()
        # Пересоздавать карточки не нужно; вкладка обычно открывается редко.
        # Если пользователь хочет актуальный отчёт — кнопка копирования берёт
        # свежие значения напрямую из engine_adapter.
