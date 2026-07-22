"""
UmbraNet - раздел «Сеть и диагностика».

Новая упрощённая версия без хаоса:
  • одно понятное состояние UmbraNet Health;
  • одна кнопка «Проверить и вылечить» вместо ручного выбора починки;
  • отдельный компактный блок DPI / WinWS;
  • ручная проверка конкретного домена оставлена как инструмент разработчика;
  • кривую массовую «проверку сервисов» убрали.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    add_query_log_event,
    bogus_force_update,
    bogus_last_updated,
    blocking_service_options,
    detect_blocking_type,
    detect_service_blocking_type,
    flush_dns_cache,
    full_diagnostics_report,
    get_active_dns_profile,
    get_engine,
    health_score,
    network_repair_soft,
    switch_mode,
)


# ════════════════════════════════════════════════════════════════════════════
# Workers: всё тяжёлое — только в фоне, чтобы вкладка не фризила UI.
# ════════════════════════════════════════════════════════════════════════════
class _HealthWorker(QThread):
    done = Signal(dict)

    def run(self):
        try:
            self.done.emit(health_score())
        except Exception as exc:  # noqa: BLE001
            self.done.emit({
                "score": 0,
                "state": "error",
                "title": "Health недоступен",
                "checks": [],
                "actions": [],
                "error": str(exc),
            })


class _AutoDoctorWorker(QThread):
    done = Signal(dict)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    @staticmethod
    def _needs_repair(hs: dict) -> tuple[bool, str]:
        """Решает, надо ли лечить и каким уровнем.

        Никакого выбора пользователю: если видим проблему DNS/IPv6 — мягко лечим.
        Если видим конфликт браузерного DoH — лечим уровнем browser.
        Агрессивное отключение IPv6 здесь НЕ делаем автоматически.
        """
        checks = hs.get("checks") or []
        need_dns = False
        need_browser = False
        for c in checks:
            title = str(c.get("title") or "")
            status = str(c.get("status") or "")
            if status not in ("warn", "error"):
                continue
            if title in ("Системный DNS", "DNS/DPI утечки"):
                need_dns = True
            if title == "Браузерный DoH":
                need_browser = True
        if need_browser:
            return True, "browser"
        if need_dns:
            return True, "soft"
        return False, "none"

    def run(self):
        result = {
            "ok": False,
            "message": "",
            "before": {},
            "after": {},
            "repair_report": {},
            "actions": [],
        }
        try:
            before = health_score()
            result["before"] = before
            if not bool(getattr(self.engine, "running", False)):
                result["message"] = "UmbraNet остановлен. Нажмите «Старт», потом автолечение сработает корректно."
                result["after"] = before
                return self.done.emit(result)

            need, level = self._needs_repair(before)
            if need:
                report = network_repair_soft(level)
                result["repair_report"] = report
                result["actions"].append(f"Запущена автопочинка уровня: {level}")
                add_query_log_event(
                    "[Автодоктор]",
                    source="fixed" if report.get("ok") else "error",
                    rcode="OK" if report.get("ok") else "WARN",
                    note=(report.get("after") or {}).get("title") or "; ".join(report.get("errors") or []) or "готово",
                )
            else:
                result["actions"].append("Лечение не потребовалось")

            after = health_score()
            result["after"] = after
            result["ok"] = int(after.get("score", 0)) >= 85
            result["message"] = after.get("title", "Проверка завершена")
        except Exception as exc:  # noqa: BLE001
            result["message"] = f"Ошибка автодоктора: {exc}"
        self.done.emit(result)


class _FullReportWorker(QThread):
    done = Signal(str)

    def run(self):
        try:
            self.done.emit(full_diagnostics_report())
        except Exception as exc:  # noqa: BLE001
            self.done.emit(f"UmbraNet Full Diagnostic Report\nОшибка: {exc}")


class _BogusUpdateWorker(QThread):
    done = Signal(bool)

    def run(self):
        try:
            self.done.emit(bool(bogus_force_update()))
        except Exception:
            self.done.emit(False)


class _BlockingDetectWorker(QThread):
    done = Signal(dict)

    def __init__(self, target: str, profile: str = "domain"):
        super().__init__()
        self.target = target
        self.profile = profile

    def run(self):
        try:
            if self.profile and self.profile != "domain":
                self.done.emit(detect_service_blocking_type(self.profile))
            else:
                self.done.emit(detect_blocking_type(self.target))
        except Exception as exc:  # noqa: BLE001
            self.done.emit({
                "domain": self.target,
                "summary": f"Ошибка проверки: {exc}",
                "severity": "error",
                "recommended_mode": "unknown",
                "checks": [],
            })


# ════════════════════════════════════════════════════════════════════════════
# Style helpers
# ════════════════════════════════════════════════════════════════════════════
def _card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setStyleSheet(f"QFrame{{{theme.card_qss(16)}}}")
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


def _mode_label(mode: str) -> str:
    return {
        "dns_only": "DNS Only",
        "combo": "Combo",
        "dpi_only": "DPI Only",
        "unknown": "неясно",
    }.get(mode, mode)


def _verdict_color(verdict: str, severity: str = "") -> str:
    if verdict == "ok" or severity == "ok":
        return theme.GREEN
    if verdict in ("dns-blocked", "dns-poisoned", "quic-blocked") or severity == "warning":
        return theme.YELLOW if verdict != "quic-blocked" else theme.ORANGE
    if verdict in ("tcp-blocked", "tls-blocked") or severity in ("problem", "error"):
        return theme.RED
    return theme.MUTED


# ════════════════════════════════════════════════════════════════════════════
# View
# ════════════════════════════════════════════════════════════════════════════
class NetworkView(QWidget):
    def __init__(self):
        super().__init__()
        self.engine = get_engine()
        self._health_worker = None
        self._doctor_worker = None
        self._report_worker = None
        self._bogus_worker = None
        self._blocking_worker = None
        self._last_health: dict | None = None
        self._last_blocking_result: dict | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("Сеть и диагностика")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:22px;font-weight:700;")
        head.addWidget(title)
        head.addStretch()
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}" + theme.scrollbar_qss())

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        lay.addWidget(self._build_overview())
        lay.addWidget(self._build_health_doctor())
        lay.addWidget(self._build_dpi_tools())
        lay.addWidget(self._build_domain_check())
        lay.addWidget(self._build_tools())
        lay.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.refresh()

    # ── builders ────────────────────────────────────────────────────────────
    def _build_overview(self):
        card, lay = _card("")
        row = QHBoxLayout()
        row.setSpacing(14)
        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(34)
        self._status_dot.setAlignment(Qt.AlignCenter)
        self._status_dot.setStyleSheet(f"color:{theme.MUTED};font-size:28px;background:transparent;border:none;")
        row.addWidget(self._status_dot)

        texts = QVBoxLayout()
        texts.setSpacing(3)
        self._status_title = QLabel("UmbraNet: —")
        self._status_title.setStyleSheet(f"color:{theme.WHITE};font-size:20px;font-weight:800;background:transparent;border:none;")
        self._status_hint = QLabel("UmbraNet сам следит за DNS/IPv6/DPI и пытается лечить типовые проблемы.")
        self._status_hint.setWordWrap(True)
        self._status_hint.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        texts.addWidget(self._status_title)
        texts.addWidget(self._status_hint)
        row.addLayout(texts, 1)

        self._profile_pill = self._pill("Профиль: —", theme.ACCENT2)
        self._transport_pill = self._pill("Транспорт: —", theme.ACCENT3)
        row.addWidget(self._profile_pill)
        row.addWidget(self._transport_pill)
        lay.addLayout(row)
        return card

    def _build_health_doctor(self):
        card, lay = _card("🩺  Автодиагностика и лечение")
        row = QHBoxLayout()
        row.setSpacing(14)

        self._health_score_label = QLabel("—")
        self._health_score_label.setFixedWidth(92)
        self._health_score_label.setAlignment(Qt.AlignCenter)
        self._health_score_label.setStyleSheet(
            f"color:{theme.MUTED};font-size:30px;font-weight:900;background:{theme.INPUT_BG};"
            f"border:1px solid {theme.BORDER};border-radius:14px;padding:10px;"
        )
        row.addWidget(self._health_score_label)

        texts = QVBoxLayout()
        texts.setSpacing(5)
        self._health_title = QLabel("Проверка ещё не запускалась")
        self._health_title.setStyleSheet(f"color:{theme.TEXT};font-size:16px;font-weight:800;background:transparent;border:none;")
        self._health_text = QLabel("Нажмите одну кнопку — UmbraNet проверит состояние и сам применит безопасную починку, если она нужна.")
        self._health_text.setWordWrap(True)
        self._health_text.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        texts.addWidget(self._health_title)
        texts.addWidget(self._health_text)
        row.addLayout(texts, 1)
        lay.addLayout(row)

        btns = QHBoxLayout()
        self._btn_doctor = self._grad_btn("🛠 Проверить и вылечить", theme.ACCENT, theme.ACCENT2, self._run_auto_doctor)
        self._btn_health_refresh = self._flat_btn("Обновить", self._refresh_health_score)
        self._btn_copy_full_report = self._flat_btn("📋 Скопировать отчёт", self._copy_full_report)
        btns.addWidget(self._btn_doctor)
        btns.addWidget(self._btn_health_refresh)
        btns.addWidget(self._btn_copy_full_report)
        btns.addStretch()
        lay.addLayout(btns)
        return card

    def _build_dpi_tools(self):
        card, lay = _card("🛡  DPI / WinWS")
        self._dpi_title = QLabel("WinWS: —")
        self._dpi_title.setStyleSheet(f"color:{theme.TEXT};font-size:15px;font-weight:700;background:transparent;border:none;")
        self._dpi_text = QLabel("Статус DPI-движка, стратегия и лог запуска.")
        self._dpi_text.setWordWrap(True)
        self._dpi_text.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(self._dpi_title)
        lay.addWidget(self._dpi_text)

        row = QHBoxLayout()
        self._btn_open_winws_log = self._flat_btn("Открыть winws.log", self._open_winws_log)
        self._btn_copy_winws_diag = self._flat_btn("📋 Скопировать DPI-диагностику", self._copy_winws_diagnostics)
        row.addWidget(self._btn_open_winws_log)
        row.addWidget(self._btn_copy_winws_diag)
        row.addStretch()
        lay.addLayout(row)
        return card

    def _build_domain_check(self):
        card, lay = _card("🔎  Проверить конкретный сайт")
        desc = QLabel("Опциональный инструмент: если один сайт не открывается, можно проверить DNS/TCP/TLS/QUIC именно для него.")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(desc)

        row = QHBoxLayout()
        self._profile_combo = QComboBox()
        for key, label in blocking_service_options():
            if key == "domain":
                self._profile_combo.addItem(label, key)
        if self._profile_combo.count() == 0:
            self._profile_combo.addItem("Домен", "domain")
        self._profile_combo.setFixedHeight(36)
        self._profile_combo.setStyleSheet(self._combo_qss())
        row.addWidget(self._profile_combo)

        self._domain_input = QLineEdit()
        self._domain_input.setPlaceholderText("youtube.com или discord.com")
        self._domain_input.setFixedHeight(36)
        self._domain_input.returnPressed.connect(self._run_blocking_detect)
        self._domain_input.setStyleSheet(
            f"QLineEdit{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 12px;font-size:13px;}}"
            f"QLineEdit:focus{{border-color:{theme.ACCENT};}}"
        )
        row.addWidget(self._domain_input, 1)
        self._btn_detect = self._grad_btn("Проверить сайт", theme.ACCENT, theme.ACCENT2, self._run_blocking_detect)
        row.addWidget(self._btn_detect)
        lay.addLayout(row)

        self._result = QLabel("Введите домен и нажмите «Проверить сайт».")
        self._result.setWordWrap(True)
        self._result.setStyleSheet(
            f"color:{theme.SUBTEXT};font-size:12px;background:{theme.INPUT_BG};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:9px;"
        )
        lay.addWidget(self._result)

        act = QHBoxLayout()
        self._btn_apply = self._flat_btn("Применить рекомендованный режим", self._apply_recommended_mode)
        self._btn_apply.setEnabled(False)
        self._btn_debug = self._flat_btn("📋 Debug сайта", self._copy_debug)
        self._btn_debug.setEnabled(False)
        act.addWidget(self._btn_apply)
        act.addWidget(self._btn_debug)
        act.addStretch()
        lay.addLayout(act)
        return card

    def _build_tools(self):
        card, lay = _card("🧰  Быстрые инструменты")
        row = QHBoxLayout()
        self._btn_flush = self._flat_btn("Сбросить DNS-кэш", self._flush_dns)
        self._btn_bogus_update = self._flat_btn("🛡 Обновить защиту от подмен", self._update_bogus_list)
        row.addWidget(self._btn_flush)
        row.addWidget(self._btn_bogus_update)
        row.addStretch()
        lay.addLayout(row)

        self._bogus_status = QLabel(self._bogus_status_text())
        self._bogus_status.setWordWrap(True)
        self._bogus_status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        lay.addWidget(self._bogus_status)
        return card

    # ── small widgets ────────────────────────────────────────────────────────
    def _pill(self, text: str, color: str):
        p = QLabel(text)
        p.setStyleSheet(
            f"color:{color};font-size:11px;font-weight:700;background:{theme.INPUT_BG};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:5px 9px;"
        )
        return p

    def _grad_btn(self, text, c1, c2, slot):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        b.setStyleSheet(
            f"QPushButton{{background:{theme.grad(c1, c2)};color:{theme.WHITE};"
            "border:none;border-radius:10px;font-weight:600;padding:0 13px;}}"
            f"QPushButton:disabled{{background:{theme.CARD};color:{theme.MUTED};border:1px solid {theme.BORDER};}}"
        )
        b.clicked.connect(slot)
        return b

    def _flat_btn(self, text, slot):
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(34)
        b.setStyleSheet(
            f"QPushButton{{background:{theme.CARD};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 13px;}}"
            f"QPushButton:hover{{border-color:{theme.ACCENT};}}"
            f"QPushButton:disabled{{color:{theme.MUTED};border-color:{theme.BORDER};}}"
        )
        b.clicked.connect(slot)
        return b

    def _combo_qss(self):
        return (
            f"QComboBox{{background:{theme.INPUT_BG};color:{theme.TEXT};"
            f"border:1px solid {theme.BORDER};border-radius:10px;padding:0 10px;min-width:150px;}}"
            f"QComboBox:hover{{border-color:{theme.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{theme.CARD};color:{theme.TEXT};selection-background-color:{theme.ACCENT};}}"
        )

    # ── Health / Doctor ─────────────────────────────────────────────────────
    def _refresh_health_score(self):
        if self._health_worker and self._health_worker.isRunning():
            return
        self._btn_health_refresh.setEnabled(False)
        self._btn_health_refresh.setText("Считаю...")
        self._health_title.setText("⏳ Проверяю состояние...")
        self._health_worker = _HealthWorker()
        self._health_worker.done.connect(self._on_health_ready)
        self._health_worker.start()

    def _on_health_ready(self, hs: dict):
        self._last_health = hs or {}
        self._btn_health_refresh.setEnabled(True)
        self._btn_health_refresh.setText("Обновить")
        if hs.get("error"):
            self._health_score_label.setText("!")
            self._health_title.setText("Health недоступен")
            self._health_text.setText(hs.get("error", "Неизвестная ошибка"))
            return

        score = int(hs.get("score", 0))
        state = hs.get("state", "warn")
        color = theme.GREEN if state == "ok" else (theme.YELLOW if state == "warn" else theme.RED)
        self._health_score_label.setText(str(score))
        self._health_score_label.setStyleSheet(
            f"color:{color};font-size:30px;font-weight:900;background:{theme.INPUT_BG};"
            f"border:1px solid {color};border-radius:14px;padding:10px;"
        )
        self._health_title.setText(hs.get("title", "Health"))
        checks = hs.get("checks") or []
        bad = [c for c in checks if c.get("status") in ("warn", "error")]
        if bad:
            text = "\n".join(f"• {c.get('title')}: {c.get('detail')}" for c in bad[:3])
        else:
            text = "Критических проблем не найдено. UmbraNet выглядит исправно."
        actions = hs.get("actions") or []
        if actions:
            text += "\nРекомендация: " + actions[0]
        self._health_text.setText(text)

    def _run_auto_doctor(self):
        if self._doctor_worker and self._doctor_worker.isRunning():
            return
        self._btn_doctor.setEnabled(False)
        self._btn_doctor.setText("Лечу...")
        self._health_title.setText("⏳ Автодоктор работает...")
        self._health_text.setText("Проверяю состояние, применяю безопасную починку при необходимости и повторно проверяю результат.")
        self._doctor_worker = _AutoDoctorWorker(self.engine)
        self._doctor_worker.done.connect(self._on_doctor_done)
        self._doctor_worker.start()

    def _on_doctor_done(self, res: dict):
        self._btn_doctor.setEnabled(True)
        self._btn_doctor.setText("🛠 Проверить и вылечить")
        after = res.get("after") or {}
        if after:
            self._on_health_ready(after)
        msg = res.get("message") or "Готово"
        actions = res.get("actions") or []
        if actions:
            msg += "\n" + "\n".join(f"• {a}" for a in actions)
        self._health_text.setText(msg)
        self._refresh_dpi_status()

    def _copy_full_report(self):
        if self._report_worker and self._report_worker.isRunning():
            return
        self._btn_copy_full_report.setEnabled(False)
        self._btn_copy_full_report.setText("Готовлю...")
        self._report_worker = _FullReportWorker()
        self._report_worker.done.connect(self._on_full_report_ready)
        self._report_worker.start()

    def _on_full_report_ready(self, text: str):
        QGuiApplication.clipboard().setText(text or "")
        self._btn_copy_full_report.setEnabled(True)
        self._btn_copy_full_report.setText("✓ Скопировано")
        QTimer.singleShot(1500, lambda: self._btn_copy_full_report.setText("📋 Скопировать отчёт"))

    # ── DPI / WinWS diagnostics ─────────────────────────────────────────────
    def _get_winws_status(self) -> dict:
        try:
            winws = getattr(self.engine, "winws", None)
            if winws is None:
                from winws_engine import get_winws_engine  # type: ignore
                winws = get_winws_engine()
            if hasattr(winws, "status"):
                return winws.status()
            return {
                "available": bool(winws and winws.is_available()),
                "running": bool(winws and winws.is_running()),
                "exe_path": str(getattr(winws, "exe_path", "")),
                "log_path": "",
                "last_error": "",
                "last_exit_code": None,
                "last_args": [],
                "last_cmd": [],
            }
        except Exception as exc:
            return {"available": False, "running": False, "last_error": str(exc), "last_args": [], "last_cmd": []}

    def _winws_log_tail(self, max_chars: int = 5000) -> str:
        try:
            path = Path(self._get_winws_status().get("log_path") or "winws.log")
            if not path.exists():
                return ""
            return path.read_text(encoding="utf-8", errors="replace")[-max_chars:].strip()
        except Exception as exc:
            return f"<не удалось прочитать winws.log: {exc}>"

    def _dpi_targets_info(self) -> dict:
        cfg = getattr(self.engine, "config", {}) or {}
        raw = list(cfg.get("routed_domains", []) or [])
        raw += list(cfg.get("subscribed_domains_set", set()) or [])
        out = []
        seen = set()
        for x in raw:
            d = str(x or "").strip().lower().strip(".")
            if not d or "." not in d:
                continue
            if d not in seen:
                seen.add(d)
                out.append(d)
        try:
            from strategy_manager import get_strategy_manager  # type: ignore
            manager = get_strategy_manager()
            path = str(getattr(manager, "active_hostlist_path", ""))
        except Exception:
            path = ""
        return {"count": len(out), "domains": out, "path": path}

    def _refresh_dpi_status(self):
        if not hasattr(self, "_dpi_title"):
            return
        cfg = getattr(self.engine, "config", {}) or {}
        targets = self._dpi_targets_info()
        st = self._get_winws_status()
        if st.get("running"):
            color = theme.GREEN
            title = "WinWS: запущен"
        elif st.get("available"):
            color = theme.YELLOW if cfg.get("dpi_mode", "off") != "off" else theme.MUTED
            title = "WinWS: остановлен"
        else:
            color = theme.RED
            title = "WinWS: не найден"
        self._dpi_title.setText(title)
        self._dpi_title.setStyleSheet(f"color:{color};font-size:15px;font-weight:700;background:transparent;border:none;")
        err = st.get("last_error") or ""
        raw_mode = cfg.get("dpi_mode", "off")
        ui_mode = {"off": "dns_only", "combo": "combo", "zapret": "dpi_only"}.get(raw_mode, "unknown")
        target_count = int(targets.get("count", 0) or 0)
        text = (
            f"Режим: {_mode_label(ui_mode)} • "
            f"Стратегия: {cfg.get('dpi_strategy', 'uz1')} • "
            f"Целей DPI: {target_count} • "
            f"Аргументов: {len(st.get('last_args') or [])}"
        )
        if cfg.get("dpi_mode", "off") != "off" and target_count == 0:
            text += "\n⚠ Цели DPI не выбраны. Включите сервисы/домены в главном меню — тогда WinWS будет работать только по ним."
        elif target_count:
            preview = ", ".join((targets.get("domains") or [])[:5])
            if target_count > 5:
                preview += f", +{target_count - 5} ещё"
            text += f"\nHostlist: {targets.get('path') or 'active_routed_hostlist.txt'}"
            text += f"\nЦели: {preview}"
        if err:
            text += f"\nПоследняя ошибка: {err[:260]}"
        else:
            text += f"\nЛог: {st.get('log_path', 'winws.log')}"
        self._dpi_text.setText(text)

    def _open_winws_log(self):
        try:
            import os
            import webbrowser
            status = self._get_winws_status()
            path = Path(status.get("log_path") or "winws.log")
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("winws.log пока пуст: WinWS ещё не запускался.\n", encoding="utf-8")
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # noqa: S606 - user-requested local file open
            else:
                webbrowser.open(path.resolve().as_uri())
            self._btn_open_winws_log.setText("✓ Открыто")
        except Exception as exc:
            self._btn_open_winws_log.setText("Ошибка")
            self._dpi_text.setText(f"Не удалось открыть winws.log: {exc}")
        QTimer.singleShot(1500, lambda: self._btn_open_winws_log.setText("Открыть winws.log"))

    def _copy_winws_diagnostics(self):
        cfg = getattr(self.engine, "config", {}) or {}
        status = self._get_winws_status()
        try:
            from strategy_manager import get_strategy_manager  # type: ignore
            manager = get_strategy_manager()
            strategy_id = cfg.get("dpi_strategy", "uz1")
            routed_targets = list(cfg.get("routed_domains", []) or [])
            routed_targets += list(cfg.get("subscribed_domains_set", set()) or [])
            args = manager.get_args(
                strategy_id,
                routed_domains=routed_targets,
                require_hostlist=(cfg.get("dpi_mode", "off") != "off"),
            )
            strategy_error = getattr(manager, "last_error", "")
        except Exception as exc:
            args = []
            strategy_error = str(exc)

        lines = [
            "UmbraNet DPI / WinWS diagnostics",
            "=" * 44,
            f"dpi_mode: {cfg.get('dpi_mode', 'off')}",
            f"dpi_strategy: {cfg.get('dpi_strategy', 'uz1')}",
            f"use_winws: {cfg.get('use_winws', True)}",
            f"engine_running: {bool(getattr(self.engine, 'running', False))}",
            "",
            f"winws_available: {status.get('available')}",
            f"winws_running: {status.get('running')}",
            f"winws_exe: {status.get('exe_path', '')}",
            f"winws_log: {status.get('log_path', '')}",
            f"last_exit_code: {status.get('last_exit_code')}",
            f"last_error: {status.get('last_error', '')}",
            "",
            f"strategy_args_count: {len(args)}",
            f"strategy_error: {strategy_error}",
            f"active_hostlist_count: {getattr(manager, 'last_hostlist_count', 0) if 'manager' in locals() else 0}",
            f"active_hostlist_path: {getattr(manager, 'active_hostlist_path', '') if 'manager' in locals() else ''}",
            f"configured_dpi_targets: {self._dpi_targets_info().get('count', 0)}",
            "strategy_args:",
        ]
        lines.extend(f"  {a}" for a in args)
        last_cmd = status.get("last_cmd") or []
        if last_cmd:
            lines += ["", "last_cmd:", "  " + " ".join(last_cmd)]
        tail = self._winws_log_tail()
        if tail:
            lines += ["", "winws.log tail:", tail]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self._btn_copy_winws_diag.setText("✓ Скопировано")
        QTimer.singleShot(1500, lambda: self._btn_copy_winws_diag.setText("📋 Скопировать DPI-диагностику"))

    # ── Domain check ────────────────────────────────────────────────────────
    def _run_blocking_detect(self):
        domain = (self._domain_input.text() or "").strip()
        profile = self._profile_combo.currentData() or "domain"
        if profile == "domain" and not domain:
            return
        if self._blocking_worker and self._blocking_worker.isRunning():
            return
        self._btn_detect.setEnabled(False)
        self._btn_detect.setText("Проверка...")
        self._btn_apply.setEnabled(False)
        self._btn_debug.setEnabled(False)
        self._result.setText("⏳ Проверяем DNS/TCP/TLS/QUIC...")
        self._last_blocking_result = None
        self._blocking_worker = _BlockingDetectWorker(domain, profile)
        self._blocking_worker.done.connect(self._on_blocking_done)
        self._blocking_worker.start()

    def _on_blocking_done(self, result: dict):
        self._last_blocking_result = result
        self._btn_detect.setEnabled(True)
        self._btn_detect.setText("Проверить сайт")
        rec = result.get("recommended_mode", "unknown")
        rec_label = _mode_label(rec)
        color = _verdict_color(result.get("verdict", ""), result.get("severity", ""))
        text = (
            f"<b>{result.get('domain', result.get('service', ''))}</b>: {result.get('summary', '')}<br>"
            f"Рекомендуемый режим: <b>{rec_label}</b>"
        )
        self._result.setText(text)
        self._result.setStyleSheet(
            f"color:{theme.TEXT};font-size:12px;background:{theme.INPUT_BG};"
            f"border:1px solid {color};border-radius:10px;padding:9px;"
        )
        if rec in ("dns_only", "combo", "dpi_only"):
            self._btn_apply.setEnabled(True)
            self._btn_apply.setText(f"Применить: {rec_label}")
        else:
            self._btn_apply.setEnabled(False)
            self._btn_apply.setText("Режим неясен")
        self._btn_debug.setEnabled(True)

    def _apply_recommended_mode(self):
        result = self._last_blocking_result or {}
        mode = result.get("recommended_mode", "unknown")
        if mode not in ("dns_only", "combo", "dpi_only"):
            self._btn_apply.setText("Режим неясен")
            QTimer.singleShot(1500, lambda: self._btn_apply.setText("Применить рекомендованный режим"))
            return
        ok, err = switch_mode(mode)
        label = _mode_label(mode)
        if ok:
            self._btn_apply.setText(f"✓ Выбран {label}")
        else:
            self._btn_apply.setText("Ошибка")
            self._result.setText(self._result.text() + f"<br><span style='color:{theme.RED};'>Не удалось применить режим: {err}</span>")
        QTimer.singleShot(1800, lambda: self._btn_apply.setText(f"Применить: {label}"))

    def _copy_debug(self):
        result = self._last_blocking_result
        if not result:
            return
        lines = ["UmbraNet Site Check Debug", "=" * 44]
        lines += [
            f"domain: {result.get('domain', '')}",
            f"verdict: {result.get('verdict', '')}",
            f"severity: {result.get('severity', '')}",
            f"recommended_mode: {result.get('recommended_mode', '')}",
            f"elapsed_ms: {result.get('elapsed_ms', '')}",
            f"summary: {result.get('summary', '')}",
            "",
            "checks:",
        ]
        for c in result.get("checks", []) or []:
            lines.append(f"- {c.get('key')} | {c.get('status')} | {c.get('title')} | {c.get('detail')}")
        QGuiApplication.clipboard().setText("\n".join(lines))
        self._btn_debug.setText("✓ Скопировано")
        QTimer.singleShot(1400, lambda: self._btn_debug.setText("📋 Debug сайта"))

    # ── Quick tools ────────────────────────────────────────────────────────
    def _bogus_status_text(self) -> str:
        ts = bogus_last_updated()
        if not ts:
            return "Защита от подмен: список ещё не обновлялся в этой сессии. Автообновление выполняется в фоне."
        dt = datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
        return f"Защита от подмен: последнее обновление {dt}."

    def _update_bogus_list(self):
        if self._bogus_worker and self._bogus_worker.isRunning():
            return
        self._btn_bogus_update.setEnabled(False)
        self._btn_bogus_update.setText("Обновляю...")
        self._bogus_status.setText("Защита от подмен: запрос обновления...")
        self._bogus_worker = _BogusUpdateWorker()
        self._bogus_worker.done.connect(self._on_bogus_done)
        self._bogus_worker.start()

    def _on_bogus_done(self, ok: bool):
        self._btn_bogus_update.setEnabled(True)
        self._btn_bogus_update.setText("🛡 Обновить защиту от подмен")
        if ok:
            self._bogus_status.setText(self._bogus_status_text())
            self._bogus_status.setStyleSheet(f"color:{theme.GREEN};font-size:12px;background:transparent;border:none;")
            add_query_log_event("[Защита от подмен]", source="fixed", rcode="OK", note="bogus-IP список обновлён")
        else:
            self._bogus_status.setText("Защита от подмен: не удалось обновить список (нет сети или сервер недоступен).")
            self._bogus_status.setStyleSheet(f"color:{theme.YELLOW};font-size:12px;background:transparent;border:none;")
            add_query_log_event("[Защита от подмен]", source="error", rcode="FAIL", note="не удалось обновить bogus-IP список")

    # ── misc ────────────────────────────────────────────────────────────────
    def _flush_dns(self):
        ok = flush_dns_cache()
        self._btn_flush.setText("✓ Сброшен" if ok else "Ошибка")
        QTimer.singleShot(1400, lambda: self._btn_flush.setText("Сбросить DNS-кэш"))

    def refresh(self):
        self.engine = get_engine()
        cfg = self.engine.config
        prof = get_active_dns_profile(cfg)
        running = bool(self.engine.running)
        if running:
            self._status_dot.setStyleSheet(f"color:{theme.GREEN};font-size:28px;background:transparent;border:none;")
            self._status_title.setText("UmbraNet работает")
            self._status_hint.setText("Автодоктор готов. Если есть проблема — нажмите «Проверить и вылечить».")
        else:
            self._status_dot.setStyleSheet(f"color:{theme.MUTED};font-size:28px;background:transparent;border:none;")
            self._status_title.setText("UmbraNet остановлен")
            self._status_hint.setText("Нажмите «Старт». После запуска UmbraNet сам проверит DNS/IPv6 и попробует исправить утечки.")
        self._profile_pill.setText(f"Профиль: {prof.get('name', '—')}")
        self._transport_pill.setText(f"Транспорт: {cfg.get('xbox_dns_mode', 'udp').upper()}")
        self._refresh_dpi_status()
        if hasattr(self, "_bogus_status"):
            self._bogus_status.setText(self._bogus_status_text())
            self._bogus_status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:12px;background:transparent;border:none;")
        self._refresh_health_score()

    def _open_test(self):
        from umbranet.widgets.dialogs import TestDnsDialog
        TestDnsDialog(self).exec()

    def _open_domain_diag(self):
        from umbranet.widgets.dialogs import DomainDiagnosticsDialog
        DomainDiagnosticsDialog(self).exec()
