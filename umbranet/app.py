"""
UmbraNet - главное окно (PySide6 / Qt Widgets).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QPainter, QRadialGradient, QColor, QCursor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget,
)

from umbranet import theme
from umbranet.engine_adapter import (
    get_engine, reset_dns_to_auto,
    switch_mode, get_current_mode,
    drain_events, is_admin, set_dns_to_localhost, get_startup_health,
    get_nav_order, set_nav_order, dpi_strategy_ai_plan, dpi_strategy_ai_run_controlled,
    dpi_strategy_ai_cleanup_runtime, dpi_strategy_check_all_controlled,
)
from umbranet.views.routing import RoutingView
from umbranet.views.profiles import ProfilesView
from umbranet.views.network import NetworkView
from umbranet.views.strategy_lab import StrategyLabView
from umbranet.views.log import LogView
from umbranet.views.settings import SettingsView
from umbranet.views.about import AboutView
from umbranet.widgets.header import ControlBar, ModeSwitch
from umbranet.widgets.sidebar import NavItem, Sidebar
from umbranet.widgets.tray import Tray

log = logging.getLogger("UmbraNet.App")

NAV_ITEMS = [
    NavItem("routing",  "Маршрутизация",      "🔀"),
    NavItem("network",  "Сеть и диагностика", "🤖"),
    NavItem("strategy_lab", "AI-стратегии",   "🧪"),
    NavItem("profiles", "DNS-профили",        "🧩"),
    NavItem("log",      "Логи",               "📑"),
    NavItem("settings", "Настройки",          "⚙"),
    NavItem("about",    "О программе",        "ℹ"),
]


def _ordered_nav_items() -> list[NavItem]:
    by_key = {it.key: it for it in NAV_ITEMS}
    order = get_nav_order([it.key for it in NAV_ITEMS])
    return [by_key[k] for k in order if k in by_key]


def _placeholder(title: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(24, 24, 24, 24)
    lbl = QLabel(f"{title}\n\n🚧 раздел в разработке")
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:18px;")
    lay.addWidget(lbl)
    return w


# ── Фоновые потоки для start/stop/restart ─────────────────────────────────────

class _EngineWorker(QThread):
    """Выполняет start/stop/restart в фоне — UI не замерзает."""
    finished = Signal(str, bool)   # (action, ok)

    def __init__(self, engine, action: str):
        super().__init__()
        self.engine = engine
        self.action = action   # "start" | "stop" | "restart"

    def run(self):
        ok = False
        try:
            if self.action == "start":
                ok = bool(self.engine.start())
            elif self.action == "stop":
                self.engine.stop()
                ok = True
            else:  # restart
                self.engine.stop()
                import time
                time.sleep(0.3)  # Даем ОС время гарантированно освободить порт 53
                ok = bool(self.engine.start())
        except Exception as exc:
            log.error("_EngineWorker(%s) ошибка: %s", self.action, exc)
            ok = False
        finally:
            # Гарантируем отправку сигнала даже при исключении,
            # чтобы _busy всегда снимался и кнопки разблокировались
            self.finished.emit(self.action, ok)


class _StartupHealthWorker(QThread):
    """Предстартовая диагностика в фоне — без фризов UI."""
    done = Signal(dict)

    def run(self):
        try:
            self.done.emit(get_startup_health())
        except Exception as exc:  # noqa: BLE001
            self.done.emit({
                "severity": "warning",
                "can_start": True,
                "summary": f"Не удалось выполнить предстартовую проверку: {exc}",
                "problems": [],
                "warnings": [str(exc)],
            })


class _AiGenerationWorker(QThread):
    """Controlled AI-generation runner в фоне."""
    progress = Signal(str)
    done = Signal(dict)

    def __init__(self, mode: str = "quick"):
        super().__init__()
        self.mode = mode

    def request_cancel(self):
        """Просит controlled AI-generation остановиться как можно быстрее."""
        self.requestInterruption()
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def run(self):
        try:
            result = dpi_strategy_ai_run_controlled(
                self.mode,
                on_progress=lambda text: self.progress.emit(str(text)),
                should_cancel=lambda: self.isInterruptionRequested(),
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "stage": "ai_generation", "error": str(exc), "created_id": ""}
        self.done.emit(result)




class _StrategyCheckWorker(QThread):
    """Controlled проверка всех Uz-стратегий."""
    progress = Signal(str)
    done = Signal(dict)

    def request_cancel(self):
        self.requestInterruption()
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def run(self):
        try:
            result = dpi_strategy_check_all_controlled(
                on_progress=lambda text: self.progress.emit(str(text)),
                should_cancel=lambda: self.isInterruptionRequested(),
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "stage": "strategy_check", "error": str(exc), "results": []}
        self.done.emit(result)


class GlowContainer(QWidget):
    """Контейнер с интерактивным световым прожектором (Spotlight Effect) на фоне.
    Плавно перемещает мягкий неоновый градиент вслед за курсором мыши,
    создавая эффект глубины и подсветки матовых карточек.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._mouse_pos = None
        self._target_pos = None
        self.setMouseTracking(True)

        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 FPS
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def _on_tick(self):
        g_pos = QCursor.pos()
        self._target_pos = self.mapFromGlobal(g_pos)

        if self._mouse_pos is None:
            self._mouse_pos = self._target_pos
        else:
            dx = self._target_pos.x() - self._mouse_pos.x()
            dy = self._target_pos.y() - self._mouse_pos.y()
            if abs(dx) > 0.5 or abs(dy) > 0.5:
                # Плавная интерполяция (easing): 0.12 - скорость следования
                self._mouse_pos.setX(int(self._mouse_pos.x() + dx * 0.12))
                self._mouse_pos.setY(int(self._mouse_pos.y() + dy * 0.12))
                self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()

        # 1. Заливаем базовым глубоким обсидиановым фоном
        p.fillRect(self.rect(), QColor(theme.BG))

        # 2. Статическая пурпурно-розовая туманность в правом верхнем углу
        p_top_right = QColor(242, 89, 176, 12)  # Розовый (PINK) с 5% непрозрачностью
        grad_tr = QRadialGradient(w - 100, 100, 400)
        grad_tr.setColorAt(0, p_top_right)
        grad_tr.setColorAt(0.5, QColor(139, 109, 255, 6))  # Лавандовый
        grad_tr.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(grad_tr)
        p.setPen(Qt.NoPen)
        p.drawEllipse(w - 400, -200, 600, 600)

        # 3. Статическая сине-бирюзовая туманность в левом нижнем углу
        p_bottom_left = QColor(52, 220, 240, 14)  # Бирюзовый (ACCENT3) с 6% непрозрачностью
        grad_bl = QRadialGradient(100, h - 100, 450)
        grad_bl.setColorAt(0, p_bottom_left)
        grad_bl.setColorAt(0.5, QColor(91, 155, 255, 8))   # Синий (ACCENT2)
        grad_bl.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(grad_bl)
        p.drawEllipse(-200, h - 400, 600, 600)

        # 4. Динамический интерактивный световой прожектор, следующий за курсором
        if self._mouse_pos is not None:
            grad = QRadialGradient(self._mouse_pos, 320)
            # Умеренные, чрезвычайно эстетичные тона
            grad.setColorAt(0, QColor(139, 109, 255, 20))  # Фиолетовый (ACCENT)
            grad.setColorAt(0.4, QColor(91, 155, 255, 8))   # Синий (ACCENT2)
            grad.setColorAt(1, QColor(0, 0, 0, 0))

            p.setBrush(grad)
            p.drawEllipse(self._mouse_pos, 320, 320)
        p.end()


class MainWindow(GlowContainer):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(theme.APP_NAME)
        # ФИХ #1: устанавливаем размер до show(), без resize() в конструкторе —
        # Qt применит его правильно после полного построения layout.
        self.setMinimumSize(theme.WIN_MIN_W, theme.WIN_MIN_H)

        self.engine = get_engine()
        # ФИХ #3: флаг занятости — защита от множественных кликов
        self._busy = False
        self._worker: _EngineWorker | None = None
        self._startup_health_worker: _StartupHealthWorker | None = None
        self._ai_generation_worker: _AiGenerationWorker | None = None
        self._strategy_check_worker: _StrategyCheckWorker | None = None
        self._last_startup_health = {"severity": "ok", "can_start": True, "summary": "Готов к запуску", "problems": [], "warnings": []}
        self._watchdog_proc = None
        # Запоминаем, меняли ли мы системный DNS в этой сессии.
        # Начиная с фикса логов DPI-режима мы переключаем DNS на UmbraNet
        # во всех режимах запуска: иначе журнал DNS-запросов в DPI Only пустой,
        # потому что Windows продолжает спрашивать DNS провайдера напрямую.
        # При остановке возвращаем DNS на DHCP только если меняли его сами.
        self._dns_was_set_by_app = False
        # AI-генерация запускается как controlled session: сначала подтверждение,
        # затем Stop, и только после успешной остановки — подготовка плана.
        self._ai_generation_pending = False
        self._strategy_check_pending = False
        
        # Фоновое автообновление доменов блокировок (YouTube, Discord и др.) с GitHub
        def _bg_update_domains():
            try:
                import threading as _threading
                # Небольшая задержка, чтобы UI успел отрисоваться
                _threading.Event().wait(3.0)
                from core.dpi.domain_updater import update_all_strategies
                from core.dpi.strategy_manager import get_strategy_manager
                update_all_strategies(get_strategy_manager().strategies_dir)
            except Exception as e:
                log.debug(f"Ошибка фонового обновления доменов: {e}")
                
        import threading as _threading
        _threading.Thread(target=_bg_update_domains, daemon=True, name="UmbraNet-DomainUpdater").start()

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar(_ordered_nav_items(), active_key="routing")
        self.sidebar.navigate.connect(self._on_navigate)
        self.sidebar.orderChanged.connect(
            lambda order: set_nav_order(order, [it.key for it in NAV_ITEMS])
        )
        root.addWidget(self.sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        topbar = self._build_topbar()
        right.addWidget(topbar)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:transparent;")
        self._views: dict[str, QWidget] = {}
        self._pages: dict[str, int] = {}
        for it in NAV_ITEMS:
            if it.key == "routing":
                page = RoutingView()
            elif it.key == "profiles":
                page = ProfilesView()
            elif it.key == "network":
                page = NetworkView()
            elif it.key == "strategy_lab":
                page = StrategyLabView()
                page.generationRequested.connect(self._on_ai_generation_requested)
                page.generationCancelRequested.connect(self._on_ai_generation_cancel_requested)
                page.strategyCheckRequested.connect(self._on_strategy_check_requested)
                page.strategyCheckCancelRequested.connect(self._on_strategy_check_cancel_requested)
            elif it.key == "log":
                page = LogView()
            elif it.key == "settings":
                page = SettingsView()
            elif it.key == "about":
                page = AboutView()
            else:
                page = _placeholder(it.label)
            self._views[it.key] = page
            idx = self.stack.addWidget(page)
            self._pages[it.key] = idx
        right.addWidget(self.stack, 1)

        right_wrap = QWidget()
        right_wrap.setStyleSheet("background:transparent;")
        right_wrap.setLayout(right)
        root.addWidget(right_wrap, 1)

        self._show("routing")

        self._really_quit = False
        self.tray = None
        if Tray.is_available():
            self.tray = Tray(self, {
                "start":   self._on_start,
                "stop":    self._on_stop,
                "restart": self._on_restart,
                "show":    self._restore_window,
                "quit":    self._quit_app,
            })
            self.tray.set_running(self.engine.running)
            self.tray.show()

        self._show_timer = QTimer(self)
        self._show_timer.setInterval(800)
        self._show_timer.timeout.connect(self._poll_show_request)
        self._show_timer.start()

        self._event_timer = QTimer(self)
        self._event_timer.setInterval(200)
        self._event_timer.timeout.connect(self._process_engine_events)
        self._event_timer.start()

        self._health_timer = QTimer(self)
        # Предстартовая диагностика может вызывать PowerShell/проверку порта,
        # поэтому не дёргаем её каждые 5 секунд — это давало микрофризы UI.
        self._health_timer.setInterval(30000)
        self._health_timer.timeout.connect(self._update_startup_health)
        self._health_timer.start()
        QTimer.singleShot(1200, self._update_startup_health)

    def showEvent(self, event):
        """ФИХ #1: задаём итоговый размер окна после первого показа.

        Qt к этому моменту уже посчитал sizeHint всех виджетов,
        поэтому resize() здесь работает корректно и не обрезает содержимое.
        """
        super().showEvent(event)
        if not hasattr(self, "_initial_resize_done"):
            self._initial_resize_done = True
            self.resize(theme.WIN_W, theme.WIN_H)

    # ── глобальная верхняя панель ──
    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet(f"background:{theme.SIDEBAR};")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(12)

        init_mode = {"dns_only": "blue", "combo": "black", "dpi_only": "red"}.get(
            get_current_mode(), "blue"
        )
        self.mode_switch = ModeSwitch(active=init_mode)
        self.mode_switch.modeChanged.connect(self._on_mode_change)
        row.addWidget(self.mode_switch)
        row.addStretch()

        self.control = ControlBar(running=self.engine.running, mode=get_current_mode())
        self.control.startClicked.connect(self._on_start)
        self.control.stopClicked.connect(self._on_stop)
        self.control.restartClicked.connect(self._on_restart)
        row.addWidget(self.control)
        lay.addLayout(row)

        self.health_banner = QLabel()
        self.health_banner.setWordWrap(True)
        self.health_banner.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.health_banner.setVisible(False)
        lay.addWidget(self.health_banner)

        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{theme.BORDER};")
        lay.addWidget(line)
        return bar

    def _format_health_details(self, health: dict) -> str:
        items = list(health.get("problems") or []) + list(health.get("warnings") or [])
        if not items:
            return health.get("summary") or "Готов к запуску"
        # В верхней панели показываем компактно, но достаточно понятно.
        return " • ".join(str(x) for x in items[:3])

    def _update_startup_health(self) -> dict:
        """Запускает предстартовую диагностику в фоне и возвращает последний результат."""
        if self._startup_health_worker and self._startup_health_worker.isRunning():
            return self._last_startup_health
        self._startup_health_worker = _StartupHealthWorker()
        self._startup_health_worker.done.connect(self._apply_startup_health)
        self._startup_health_worker.start()
        return self._last_startup_health

    def _apply_startup_health(self, health: dict):
        self._last_startup_health = health or self._last_startup_health
        severity = self._last_startup_health.get("severity", "ok")
        if severity == "ok":
            self.health_banner.setVisible(False)
            return

        if severity == "error":
            icon, color, bg = "⛔", theme.RED, "rgba(255, 100, 120, 0.12)"
            title = "UmbraNet не готов к запуску"
        else:
            icon, color, bg = "⚠", theme.YELLOW, "rgba(251, 191, 36, 0.12)"
            title = "Есть предупреждения"

        self.health_banner.setText(
            f"{icon} <b>{title}</b>: {self._format_health_details(self._last_startup_health)}"
        )
        self.health_banner.setStyleSheet(
            f"QLabel{{"
            f"background:{bg}; color:{theme.TEXT};"
            f"border:1px solid {color}; border-radius:10px;"
            f"padding:8px 10px; font-size:12px;"
            f"}}"
        )
        self.health_banner.setVisible(True)


    def _update_mode_hint(self):
        """Подсказки режимов отключены по просьбе пользователя."""
        return


    # ── режим DPI ──
    def _on_mode_change(self, ui_key: str):
        UI_KEY_TO_MODE = {
            "blue":  "dns_only",
            "black": "combo",
            "red":   "dpi_only",
        }
        ui_mode = UI_KEY_TO_MODE.get(ui_key, "dns_only")
        
        # Если пытаемся сменить режим пока программа работает - просто останавливаем ее
        if self.engine.running:
            self._on_stop()

        ok, err = switch_mode(ui_mode)
        if not ok:
            log.warning("Не удалось переключить режим '%s': %s", ui_mode, err)
            actual_key = {"dns_only": "blue", "combo": "black", "dpi_only": "red"}.get(
                get_current_mode(), "blue"
            )
            self.mode_switch.set_active(actual_key)
        self._update_mode_hint()
        QTimer.singleShot(400, self._update_startup_health)
        
        # Обновляем все UI-компоненты (вкладку Маршрутизация и т.д.),
        # чтобы они переключились с DNS-маршрутов на DPI-стратегии.
        self._refresh_views()

    # ── ФИХ #3 + #6: старт/стоп/рестарт через фоновый поток ──
    def _start_action(self, action: str):
        """Запускает action в фоновом потоке с блокировкой повторных кликов."""
        if self._busy:
            return
        # Явный пользовательский Stop должен отменять любые фоновые мягкие
        # restart-задачи из вкладок. Иначе worker маршрутизации мог остановить
        # engine, а затем снова стартовать его уже после нажатия «Стоп».
        try:
            setattr(self.engine, "_manual_stop_requested", action == "stop")
        except Exception:
            pass
        # Не запускаем тяжёлую предстартовую диагностику в UI-потоке по нажатию
        # «Старт»: сам engine.start() делает preflight в рабочем потоке и вернёт
        # понятную ошибку через last_start_error. Это убирает зависание кнопки.
        self._busy = True
        self.control.set_busy(action)          # показываем промежуточный статус
        if self.tray:
            self.tray.set_waiting(
                {"start": "Запуск...", "stop": "Остановка...", "restart": "Перезапуск..."}
                .get(action, "...")
            )
        self._worker = _EngineWorker(self.engine, action)
        self._worker.finished.connect(self._on_action_done)
        self._worker.start()

    def _on_start(self):
        self._start_action("start")

    def _on_stop(self):
        self._start_action("stop")

    def _on_restart(self):
        self._start_action("restart")


    def _on_strategy_check_requested(self):
        """Controlled check-all: при необходимости сначала останавливаем UmbraNet."""
        view = self._views.get("strategy_lab")
        if self._busy:
            if hasattr(view, "_generation_busy"):
                view._generation_busy()
            return
        was_running = bool(getattr(self.engine, "running", False))
        self._strategy_check_pending = True
        if hasattr(view, "_check_stop_started"):
            view._check_stop_started(was_running)
        if was_running:
            self._on_stop()
        else:
            self._begin_strategy_check_session()

    def _begin_strategy_check_session(self):
        view = self._views.get("strategy_lab")
        try:
            if self._strategy_check_worker and self._strategy_check_worker.isRunning():
                if hasattr(view, "_generation_busy"):
                    view._generation_busy()
                return
            self._busy = True
            if hasattr(self.control, "set_ai_busy"):
                self.control.set_ai_busy()
            if hasattr(view, "_check_plan_ready"):
                view._check_plan_ready()
            self._strategy_check_worker = _StrategyCheckWorker()
            self._strategy_check_worker.progress.connect(self._on_strategy_check_progress)
            self._strategy_check_worker.done.connect(self._on_strategy_check_done)
            self._strategy_check_worker.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Strategy check start failed: %s", exc)
            self._busy = False
            self._strategy_check_pending = False
            self.control.set_running(bool(getattr(self.engine, "running", False)), mode=get_current_mode())
            if hasattr(view, "_generation_plan_error"):
                view._generation_plan_error(str(exc))

    def _on_strategy_check_progress(self, text: str):
        view = self._views.get("strategy_lab")
        if hasattr(view, "_check_progress"):
            view._check_progress(text)

    def _on_strategy_check_cancel_requested(self):
        try:
            w = getattr(self, "_strategy_check_worker", None)
            if w is not None and w.isRunning():
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
                else:
                    w.requestInterruption()
                dpi_strategy_ai_cleanup_runtime()
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("Strategy check cancel failed: %s", exc)
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def _on_strategy_check_done(self, result: dict):
        view = self._views.get("strategy_lab")
        try:
            cleanup = dpi_strategy_ai_cleanup_runtime()
            lines = list(result.get("report_lines") or [])
            stopped = ", ".join(cleanup.get("stopped") or []) or "ничего не осталось"
            errors = "; ".join(cleanup.get("errors") or [])
            lines.append(f"DPI cleanup: {stopped}" + (f" • ошибки: {errors}" if errors else " • OK"))
            result["report_lines"] = lines
            result["cleanup"] = cleanup
        except Exception as exc:
            result["cleanup"] = {"stopped": [], "errors": [str(exc)]}
        self._busy = False
        self._strategy_check_pending = False
        self.control.set_running(False, mode=get_current_mode())
        if self.tray:
            self.tray.set_running(bool(getattr(self.engine, "running", False)))
        if hasattr(view, "_check_finished"):
            view._check_finished(result)
        self._refresh_current_view()

    def _on_ai_generation_requested(self):
        """Первый шаг controlled AI-generation: подтверждение из вкладки → Stop.

        Реальный исполнитель генерации будет подключён следующим этапом; сейчас
        важно безопасно пройти UX и остановить текущие процессы тем же путём,
        что и кнопка «Стоп».
        """
        view = self._views.get("strategy_lab")
        if self._busy:
            if hasattr(view, "_generation_busy"):
                view._generation_busy()
            return
        was_running = bool(getattr(self.engine, "running", False))
        self._ai_generation_pending = True
        if hasattr(view, "_generation_stop_started"):
            view._generation_stop_started(was_running)
        if was_running:
            self._on_stop()
        else:
            self._begin_ai_generation_session()

    def _begin_ai_generation_session(self):
        """Готовит план и запускает controlled AI-generation worker."""
        view = self._views.get("strategy_lab")
        try:
            plan = dpi_strategy_ai_plan("quick")
            if hasattr(view, "_generation_plan_ready"):
                view._generation_plan_ready(plan)
            if self._ai_generation_worker and self._ai_generation_worker.isRunning():
                if hasattr(view, "_generation_busy"):
                    view._generation_busy()
                return
            self._busy = True
            if hasattr(self.control, "set_ai_busy"):
                self.control.set_ai_busy()
            self._ai_generation_worker = _AiGenerationWorker("quick")
            self._ai_generation_worker.progress.connect(self._on_ai_generation_progress)
            self._ai_generation_worker.done.connect(self._on_ai_generation_done)
            self._ai_generation_worker.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("AI generation start failed: %s", exc)
            self._busy = False
            self.control.set_running(bool(getattr(self.engine, "running", False)), mode=get_current_mode())
            if hasattr(view, "_generation_plan_error"):
                view._generation_plan_error(str(exc))
            self._ai_generation_pending = False

    def _on_ai_generation_progress(self, text: str):
        view = self._views.get("strategy_lab")
        if hasattr(view, "_generation_progress"):
            view._generation_progress(text)

    def _on_ai_generation_cancel_requested(self):
        """Отмена AI-генерации из UI: не закрываем программу, только DPI-session."""
        try:
            w = getattr(self, "_ai_generation_worker", None)
            if w is not None and w.isRunning():
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
                else:
                    w.requestInterruption()
                dpi_strategy_ai_cleanup_runtime()
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("AI generation cancel failed: %s", exc)
        # Если worker уже завершился между кликом и обработкой — всё равно чистим DPI.
        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception:
            pass

    def _on_ai_generation_done(self, result: dict):
        view = self._views.get("strategy_lab")
        # Safety net: controlled generation must never leave WinWS/engine running
        # after completion. User will start UmbraNet manually when ready.
        cleanup = {"stopped": [], "errors": []}
        try:
            cleanup = dpi_strategy_ai_cleanup_runtime()
            result["cleanup"] = cleanup
            lines = list(result.get("report_lines") or [])
            stopped = ", ".join(cleanup.get("stopped") or []) or "ничего не осталось"
            errors = "; ".join(cleanup.get("errors") or [])
            lines.append(f"DPI cleanup: {stopped}" + (f" • ошибки: {errors}" if errors else " • OK"))
            result["report_lines"] = lines
            if isinstance(result.get("report"), dict):
                result["report"]["lines"] = lines
                result["report"]["cleanup"] = cleanup
            if cleanup.get("stopped"):
                log.info("AI generation cleanup stopped: %s", cleanup.get("stopped"))
        except Exception as exc:  # noqa: BLE001
            result["cleanup"] = {"stopped": [], "errors": [str(exc)]}
            log.warning("AI generation cleanup failed: %s", exc)
        self._busy = False
        self._ai_generation_pending = False
        self._strategy_check_pending = False
        self.control.set_running(False, mode=get_current_mode())
        if self.tray:
            self.tray.set_running(bool(getattr(self.engine, "running", False)))
        if hasattr(view, "_generation_finished"):
            view._generation_finished(result)
        self._refresh_current_view()

    def _on_action_done(self, action: str, ok: bool):
        """Вызывается из фонового потока по завершении start/stop/restart."""
        self._busy = False
        running = self.engine.running
        self.control.set_running(running, mode=get_current_mode())
        if self.tray:
            self.tray.set_running(running)

        if ok and action in ("start", "restart") and running:
            current_mode = get_current_mode()
            if is_admin():
                # ВАЖНО: системный DNS переключаем на 127.0.0.1 во всех режимах,
                # включая DPI Only. Иначе в красном режиме сам DPI/WinWS работает,
                # но Windows отправляет DNS-запросы мимо UmbraNet, поэтому вкладка
                # «Логи запросов» остаётся пустой. DNS-сервер всё равно запущен
                # и нужен для корректного резолва/журнала.
                import threading as _threading
                import subprocess
                import os

                # Запускаем watchdog, чтобы он следил за нами
                if getattr(self, "_watchdog_proc", None) is None:
                    try:
                        import sys
                        core_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core")
                        wd_path = os.path.join(core_dir, "watchdog.py")
                        if os.path.exists(wd_path):
                            python_exe = sys.executable
                            if "python.exe" in python_exe.lower() and "pythonw.exe" not in python_exe.lower():
                                pw = python_exe.lower().replace("python.exe", "pythonw.exe")
                                if os.path.exists(pw):
                                    python_exe = pw
                            self._watchdog_proc = subprocess.Popen(
                                [python_exe, wd_path, str(os.getpid())],
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                            )
                            log.info(f"Запущен Watchdog (PID {self._watchdog_proc.pid}) для защиты DNS")
                    except Exception as exc:
                        log.warning(f"Не удалось запустить watchdog: {exc}")

                cfg = self.engine.config
                def _set_dns():
                    dns_ok, dns_msg, _ = set_dns_to_localhost(
                        fallback_ipv4=cfg.get("fallback_dns", "1.1.1.1"),
                        fallback_ipv6=cfg.get("fallback_dns6", ""),
                        enable_ipv6=cfg.get("enable_ipv6", True),
                    )
                    if not dns_ok:
                        log.warning("set_dns_to_localhost не удалось: %s", dns_msg)
                    else:
                        self._dns_was_set_by_app = True
                        log.info("Системный DNS переключён на UmbraNet для режима %s", current_mode)
                        # После смены DNS запускаем общий автодоктор в фоне:
                        # Health → если надо repair → Health → запись в логи.
                        _threading.Thread(
                            target=self._auto_doctor_after_start,
                            daemon=True, name="UmbraNet-AutoDoctor"
                        ).start()
                _threading.Thread(target=_set_dns, daemon=True, name="UmbraNet-SetDNS").start()
            else:
                # Без прав администратора нельзя сменить системный DNS —
                # логи DNS-запросов в DPI Only/Combo могут быть пустыми.
                self.control.set_running(True, mode=get_current_mode(), admin_warn=True)

        elif ok and action == "stop":
            # Добиваем временные/оторванные DPI-процессы. Обычный engine.stop()
            # не всегда видит orphan winws.exe, а именно он держит WinDivert.
            try:
                dpi_strategy_ai_cleanup_runtime()
            except Exception as exc:
                log.debug("cleanup после Stop не удался: %s", exc)

            # При остановке убиваем watchdog
            if getattr(self, "_watchdog_proc", None) is not None:
                try:
                    self._watchdog_proc.kill()
                    self._watchdog_proc = None
                except Exception:
                    pass
                    
            # Возвращаем системный DNS на DHCP только если именно мы меняли его
            # в этой сессии.
            if self._dns_was_set_by_app and is_admin():
                import threading as _threading
                def _reset():
                    reset_dns_to_auto()
                    self._dns_was_set_by_app = False
                _threading.Thread(
                    target=_reset, daemon=True, name="UmbraNet-ResetDNS"
                ).start()

        if action == "stop" and self._ai_generation_pending:
            if ok:
                # Даём UI и фоновому DNS-reset короткий тик, затем готовим план.
                QTimer.singleShot(350, self._begin_ai_generation_session)
            else:
                view = self._views.get("strategy_lab")
                if hasattr(view, "_generation_plan_error"):
                    view._generation_plan_error("Не удалось остановить UmbraNet перед генерацией")
                self._ai_generation_pending = False

        if action == "stop" and self._strategy_check_pending:
            if ok:
                QTimer.singleShot(350, self._begin_strategy_check_session)
            else:
                view = self._views.get("strategy_lab")
                if hasattr(view, "_generation_plan_error"):
                    view._generation_plan_error("Не удалось остановить UmbraNet перед проверкой стратегий")
                self._strategy_check_pending = False

        if not ok and action in ("start", "restart"):
            self.control.set_error(self.engine.last_start_error or "Не удалось запустить")
        QTimer.singleShot(300, self._update_startup_health)
        self._refresh_current_view()

    def _auto_doctor_after_start(self):
        """Фоновый автодоктор после успешного старта.

        Идея: пользователь не должен руками выбирать «что чинить». После старта
        UmbraNet сам проверяет Health, применяет безопасную починку при нужде и
        пишет итог в QueryLog/события UI.
        """
        import time
        time.sleep(2.0)  # ждём DNS/WinWS и применение DNS-настроек Windows
        try:
            from umbranet.engine_adapter import (
                add_query_log_event,
                health_score,
                network_repair_soft,
                post_event,
            )

            def _choose_repair_level(hs: dict) -> tuple[bool, str]:
                checks = hs.get("checks") or []
                need_dns = False
                need_browser = False
                for c in checks:
                    status = str(c.get("status") or "")
                    title = str(c.get("title") or "")
                    if status not in ("warn", "error"):
                        continue
                    if title in ("Системный DNS", "DNS/DPI утечки"):
                        need_dns = True
                    elif title == "Браузерный DoH":
                        need_browser = True
                if need_browser:
                    return True, "browser"
                if need_dns:
                    return True, "soft"
                return False, "none"

            before = health_score()
            before_score = int(before.get("score", 0) or 0)
            before_title = before.get("title", "Health")
            add_query_log_event(
                "[Автодоктор: проверка]",
                source="check" if before_score >= 85 else "leak",
                rcode="OK" if before_score >= 85 else "WARN",
                note=f"Health {before_score}/100 — {before_title}",
            )

            need, level = _choose_repair_level(before)
            repair_report = None
            if need:
                log.info("Автодоктор: требуется починка уровня %s", level)
                repair_report = network_repair_soft(level)
                add_query_log_event(
                    "[Автодоктор: лечение]",
                    source="fixed" if repair_report.get("ok") else "error",
                    rcode="OK" if repair_report.get("ok") else "WARN",
                    note=(repair_report.get("after") or {}).get("title")
                         or "; ".join(repair_report.get("errors") or [])
                         or f"уровень {level}",
                )
            else:
                log.info("Автодоктор: лечение не требуется (%s/100)", before_score)

            after = health_score()
            after_score = int(after.get("score", 0) or 0)
            after_title = after.get("title", "Health")
            add_query_log_event(
                "[Автодоктор: итог]",
                source="fixed" if after_score >= before_score else "error",
                rcode="OK" if after_score >= 85 else "WARN",
                note=f"Health {before_score} → {after_score}/100 — {after_title}",
            )
            post_event({
                "type": "auto_doctor_done",
                "score_before": before_score,
                "score_after": after_score,
                "title": after_title,
                "repaired": bool(need),
                "level": level,
                "ok": after_score >= 85,
                "message": f"Автодоктор: Health {before_score} → {after_score}/100",
            })
        except Exception as exc:
            log.debug("_auto_doctor_after_start ошибка: %s", exc)
            try:
                from umbranet.engine_adapter import add_query_log_event, post_event
                add_query_log_event(
                    "[Автодоктор: ошибка]",
                    source="error",
                    rcode="FAIL",
                    note=str(exc),
                )
                post_event({"type": "auto_doctor_done", "ok": False, "message": f"Автодоктор ошибка: {exc}"})
            except Exception:
                pass

    def _refresh_views(self):
        """Обновляет только текущую вкладку.

        Раньше при смене режима мы refresh'или все вкладки, включая скрытую
        «AI-стратегии». На Windows/PySide перестройка скрытых QWidget-списков
        может давать микро-окна/мигание. Скрытые вкладки обновятся при входе в
        них через _show().
        """
        self._refresh_current_view()

    def _refresh_current_view(self):
        try:
            idx = self.stack.currentIndex()
            for key, page_idx in self._pages.items():
                if page_idx == idx:
                    v = self._views.get(key)
                    if v is not None and hasattr(v, "refresh"):
                        v.refresh()
                    return
        except Exception:
            pass

    def _on_navigate(self, key: str):
        self._show(key)

    def _restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _process_engine_events(self):
        for event in drain_events():
            etype = event.get("type")
            if etype == "status_changed":
                running = bool(event.get("running", False))
                if not self._busy:
                    self.control.set_running(running, mode=get_current_mode())
                    if self.tray:
                        self.tray.set_running(running)
                self._update_startup_health()
                self._refresh_current_view()
            elif etype == "mode_changed":
                mode = event.get("mode", "dns_only")
                ui_key = {"dns_only": "blue", "combo": "black", "dpi_only": "red"}.get(mode, "blue")
                self.mode_switch.set_active(ui_key)
                self._update_mode_hint()
            elif etype == "auto_doctor_done":
                msg = event.get("message", "Автодоктор завершён")
                ok = bool(event.get("ok", False))
                log.info("%s", msg)
                if self.tray:
                    try:
                        self.tray.notify(("✅ " if ok else "⚠ ") + msg)
                    except Exception:
                        pass
                self._refresh_current_view()
            elif etype == "error":
                msg = event.get("message", "Неизвестная ошибка")
                log.warning("Событие ошибки от движка: %s", msg)

    def _poll_show_request(self):
        try:
            from single_instance import consume_show_request  # type: ignore
            if consume_show_request():
                self._restore_window()
        except Exception:
            pass

    def _stop_view_workers(self):
        try:
            nv = self._views.get("network")
            if nv is not None:
                w = getattr(nv, "_diag_worker", None)
                if w is not None and w.isRunning():
                    w.wait(2000)
        except Exception:
            pass
        try:
            rv = self._views.get("routing")
            if rv is not None:
                if hasattr(rv, "_ping_timer"):
                    rv._ping_timer.stop()
                if hasattr(rv, "_transport_list"):
                    rv._transport_list.stop_workers()
        except Exception:
            pass

    def _hard_stop_runtime(self, reset_dns: bool = False):
        """Единая жёсткая остановка runtime для выхода/удаления/аварий.

        Останавливает фоновые UI-worker'ы, engine, WinWS/orphan WinWS и watchdog.
        При reset_dns=True дополнительно возвращает системный DNS на DHCP.
        """
        try:
            setattr(self.engine, "_manual_stop_requested", True)
        except Exception:
            pass
        for tname in ("_show_timer", "_event_timer", "_health_timer"):
            try:
                t = getattr(self, tname, None)
                if t is not None:
                    t.stop()
            except Exception:
                pass
        self._stop_view_workers()

        # Если пользователь выходит/удаляет программу во время генерации DPI-
        # стратегий, рабочий QThread раньше мог продолжить цикл и снова поднять
        # winws.exe уже после общей очистки. Запрашиваем отмену и ждём коротко:
        # runner проверяет флаг между вариантами/пробами и сам добивает WinWS.
        try:
            w = getattr(self, "_ai_generation_worker", None)
            if w is not None and w.isRunning():
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
                else:
                    w.requestInterruption()
                w.wait(15000)
        except Exception:
            pass
        try:
            w = getattr(self, "_strategy_check_worker", None)
            if w is not None and w.isRunning():
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
                else:
                    w.requestInterruption()
                w.wait(15000)
        except Exception:
            pass

        if getattr(self, "_watchdog_proc", None) is not None:
            try:
                self._watchdog_proc.kill()
            except Exception:
                pass
            self._watchdog_proc = None

        try:
            dpi_strategy_ai_cleanup_runtime()
        except Exception as exc:
            log.debug("hard cleanup runtime ошибка: %s", exc)

        try:
            self.engine.stop()
        except Exception:
            pass

        if reset_dns and is_admin():
            try:
                reset_dns_to_auto()
                self._dns_was_set_by_app = False
            except Exception as exc:
                log.debug("hard reset DNS ошибка: %s", exc)

    def _quit_app(self):
        self._really_quit = True
        # При настоящем выходе всегда чистим runtime и DNS. Это важно для
        # удаления папки программы и чтобы после выхода не оставался WinDivert/winws.
        self._hard_stop_runtime(reset_dns=True)
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if self.tray and not self._really_quit:
            event.ignore()
            self.hide()
            self.tray.notify("UmbraNet свёрнут в трей. DNS продолжает работать.")
        else:
            # Если это реальный выход (не сворачивание в трей), чистим runtime.
            # reset_dns=True нужен и для сценария без трея/принудительного закрытия.
            self._hard_stop_runtime(reset_dns=True)
            event.accept()

    def _show(self, key: str):
        if key in self._pages:
            self.stack.setCurrentIndex(self._pages[key])
            v = self._views.get(key)
            if v is not None and hasattr(v, "refresh"):
                try:
                    v.refresh()
                except Exception:
                    pass


def run():
    import sys

    guard = None
    try:
        from single_instance import SingleInstance, request_show_existing  # type: ignore
        guard = SingleInstance("UmbraNet_SingleInstance_Mutex_v1")
        if guard.already_running():
            request_show_existing()
            print("UmbraNet уже запущен — показываю существующее окно.")
            return
    except Exception:
        guard = None

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10))
    try:
        from umbranet.ru_clipboard import install_ru_clipboard
        install_ru_clipboard(app)
    except Exception:
        pass
    win = MainWindow()
    win.show()
    try:
        code = app.exec()
    finally:
        if guard is not None:
            try:
                guard.release()
            except Exception:
                pass
    sys.exit(code)
