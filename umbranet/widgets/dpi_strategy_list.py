"""
UmbraNet - Список DPI-стратегий в главном меню.

Важно: список показывает ТОЛЬКО стратегии из реальной папки UmbraNet/strategies.
Никаких встроенных/fallback стратегий вроде General здесь быть не должно.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from umbranet import theme
from umbranet import engine_adapter as ea


class DpiStrategyList(QWidget):
    strategyChanged = Signal(str)

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background:transparent;border:none;")

        self._rows: dict[str, QFrame] = {}
        self._active = str(ea.get_engine().config.get("dpi_strategy", "uz1")).lower()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}"
            + theme.scrollbar_qss()
        )

        self._scroll_widget = QWidget()
        self._rows_lay = QVBoxLayout(self._scroll_widget)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(5)
        self._scroll.setWidget(self._scroll_widget)

        _row_h = 48
        self._scroll.setFixedHeight(7 * _row_h + 6 * 5)
        lay.addWidget(self._scroll)

        self._rebuild_rows()

    def _strategy_items(self) -> list[dict]:
        """Единый источник списка: engine_adapter читает UmbraNet/strategies."""
        return list(ea.dpi_strategy_items() or [])

    def _clear_rows(self):
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()
        self._rows.clear()

    def _rebuild_rows(self):
        self._clear_rows()
        strategies = self._strategy_items()

        if not strategies:
            empty = QLabel("Стратегий нет в папке strategies")
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color:{theme.MUTED};font-size:12px;background:{theme.ROW_BG};"
                f"border:1px solid {theme.BORDER};border-radius:9px;padding:14px;"
            )
            self._rows_lay.addWidget(empty)
            self._rows_lay.addStretch()
            return

        for strat in strategies:
            key = str(strat.get("id", "")).strip().lower()
            if not key:
                continue
            row = self._make_row(strat)
            self._rows[key] = row
            self._rows_lay.addWidget(row)
        self._rows_lay.addStretch()
        self._restyle()

    def _make_row(self, strategy: dict) -> QFrame:
        key = str(strategy.get("id", "")).strip().lower()
        row = QFrame()
        row.setCursor(Qt.PointingHandCursor)

        rl = QVBoxLayout(row)
        rl.setContentsMargins(10, 5, 10, 5)
        rl.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)

        dot = QLabel("●")
        dot.setObjectName("dot")
        dot.setStyleSheet(f"color:{theme.MUTED};font-size:10px;background:transparent;border:none;")
        top.addWidget(dot)

        name = QLabel(str(strategy.get("name", key)))
        name.setObjectName("name")
        name.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;border:none;")
        top.addWidget(name)
        top.addStretch()
        rl.addLayout(top)

        desc = QLabel(str(strategy.get("description", "") or ""))
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#b2b3d6;font-size:10.5px;background:transparent;border:none;")
        rl.addWidget(desc)

        # Дочерние QLabel не должны мешать клику по всей строке.
        for child in row.findChildren(QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        row.mousePressEvent = lambda _e, k=key: self._select(k)
        return row

    def _select(self, key: str):
        key = str(key or "").strip().lower()
        if not key or key == self._active:
            return

        ok, msg = ea.dpi_strategy_set_active(key)
        if not ok:
            import logging
            logging.getLogger("UmbraNet.DpiStrategyList").warning("Не удалось выбрать стратегию %s: %s", key, msg)
            return

        self._active = key

        # Перезапускаем WinWS, если DPI сейчас активен, чтобы применить стратегию.
        try:
            eng = ea.get_engine()
            current_mode = eng.config.get("dpi_mode", "off")
            if current_mode != "off" and getattr(eng, "winws", None) and eng.winws.is_running():
                from strategy_manager import StrategyManager  # type: ignore
                manager = StrategyManager(ea.get_strategies_dir())
                routed_targets = list(eng.config.get("routed_domains", []) or [])
                routed_targets += list(eng.config.get("subscribed_domains_set", set()) or [])
                args = manager.get_args(
                    key,
                    routed_domains=routed_targets,
                    require_hostlist=True,
                )
                if args:
                    import logging
                    logging.getLogger("UmbraNet.DpiStrategyList").info("Смена стратегии на %s, перезапуск WinWS", key)
                    eng.winws.restart(args)
                else:
                    import logging
                    logging.getLogger("UmbraNet.DpiStrategyList").warning(
                        "WinWS не перезапущен: %s", manager.last_error
                    )
        except Exception as exc:
            import logging
            logging.getLogger("UmbraNet.DpiStrategyList").error("Ошибка перезапуска WinWS: %s", exc)

        self._restyle()
        self.strategyChanged.emit(key)

    def refresh(self):
        self._active = str(ea.get_engine().config.get("dpi_strategy", "uz1")).lower()
        self._rebuild_rows()

    def _restyle(self):
        for key, row in self._rows.items():
            is_active = (key == self._active)
            dot = row.findChild(QLabel, "dot")
            name = row.findChild(QLabel, "name")
            desc = row.findChild(QLabel, "desc")

            if is_active:
                row.setStyleSheet(
                    "QFrame{background:" + theme.grad(theme.CARD_TOP, theme.ROW_BG, False) + ";"
                    f"border:1px solid {theme.ACCENT};border-radius:9px;}}"
                    "QLabel{background:transparent;border:none;}"
                )
                if dot:
                    dot.setStyleSheet(f"color:{theme.GREEN};font-size:10px;background:transparent;border:none;")
            else:
                row.setStyleSheet(
                    f"QFrame{{background:{theme.ROW_BG};border:1px solid {theme.BORDER};border-radius:9px;}}"
                    f"QFrame:hover{{border-color:{theme.ACCENT};}}"
                    "QLabel{background:transparent;border:none;}"
                )
                if dot:
                    dot.setStyleSheet(f"color:{theme.MUTED};font-size:10px;background:transparent;border:none;")

            if name:
                name.setStyleSheet(f"color:{theme.TEXT};font-size:12px;font-weight:700;background:transparent;border:none;")
            if desc:
                desc.setStyleSheet("color:#b2b3d6;font-size:10.5px;background:transparent;border:none;")
