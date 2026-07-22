"""
UmbraNet - график латентности DNS.

Поддерживает несколько видов отображения:
  • smooth  — плавная неоновая линия;
  • angular — угловатая линия как в старой версии;
  • bars    — биржевой стиль с вертикальными палочками в цветах UmbraNet.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from umbranet import theme


class Sparkline(QWidget):
    def __init__(self, capacity: int = 40, mode: str = "bars", grid_size: int = 5):
        super().__init__()
        self._data: deque[float] = deque(maxlen=capacity)
        self._mode = mode if mode in ("smooth", "angular", "bars") else "bars"
        self._grid_size = max(2, min(10, int(grid_size or 5)))
        self.setMinimumHeight(90)

    def set_mode(self, mode: str):
        mode = str(mode).lower()
        if mode in ("smooth", "angular", "bars"):
            self._mode = mode
            self.update()

    def set_grid_size(self, grid_size: int):
        self._grid_size = max(2, min(10, int(grid_size or 5)))
        self.update()

    def push(self, value: float):
        self._data.append(max(0.0, float(value)))
        self.update()

    def set_data(self, values: list[float]):
        self._data.clear()
        for v in values:
            self._data.append(max(0.0, float(v)))
        self.update()

    @property
    def last(self) -> float | None:
        return self._data[-1] if self._data else None

    @property
    def avg(self) -> float | None:
        return sum(self._data) / len(self._data) if self._data else None

    @staticmethod
    def _c(hex_color: str, alpha: int | None = None) -> QColor:
        c = QColor(hex_color)
        if alpha is not None:
            c.setAlpha(alpha)
        return c

    @staticmethod
    def _bounds(values: list[float]) -> tuple[float, float]:
        vmax = max(values) if values else 100.0
        vmin = min(values) if values else 0.0
        if vmax == vmin:
            pad = max(8.0, vmax * 0.25)
            return max(0.0, vmin - pad), vmax + pad
        span = vmax - vmin
        pad = max(4.0, span * 0.22)
        return max(0.0, vmin - pad), vmax + pad

    @staticmethod
    def _smooth_path(points: list[QPointF]) -> QPainterPath:
        path = QPainterPath(points[0])
        if len(points) == 2:
            path.lineTo(points[1])
            return path
        for i in range(1, len(points)):
            p0 = points[i - 1]
            p1 = points[i]
            mid_x = (p0.x() + p1.x()) / 2.0
            path.cubicTo(QPointF(mid_x, p0.y()), QPointF(mid_x, p1.y()), p1)
        return path

    def _draw_grid(self, p: QPainter, chart: QRectF, vmin: float, vmax: float):
        bg = QLinearGradient(chart.topLeft(), chart.bottomLeft())
        bg.setColorAt(0, self._c(theme.CARD_TOP, 46))
        bg.setColorAt(1, self._c(theme.CARD_DARK, 15))
        p.fillRect(chart, bg)

        grid = self._grid_size
        grid_pen = QPen(self._c(theme.BORDER, 95), 1)
        grid_pen.setStyle(Qt.DotLine)
        p.setPen(grid_pen)
        p.setFont(QFont("Consolas", 7))

        for i in range(grid + 1):
            y = chart.top() + chart.height() * i / grid
            p.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))
            val = vmax - (vmax - vmin) * i / grid
            p.setPen(self._c(theme.MUTED, 180))
            p.drawText(QRectF(chart.right() - 40, y - 8, 38, 14), Qt.AlignRight | Qt.AlignVCenter, str(int(val)))
            p.setPen(grid_pen)

        for i in range(1, grid + 1):
            x = chart.left() + chart.width() * i / grid
            p.drawLine(QPointF(x, chart.top()), QPointF(x, chart.bottom()))

        p.setPen(QPen(self._c(theme.BORDER, 150), 1))
        p.drawLine(chart.bottomLeft(), chart.bottomRight())

    def _points(self, vals: list[float], chart: QRectF, vmin: float, vmax: float) -> list[QPointF]:
        span = (vmax - vmin) or 1.0
        # Та же логика, что у «биржевых палочек»: позиции идут по фиксированным
        # слотам capacity. Новые точки появляются справа, старые уходят влево,
        # а линия не растягивается/не сжимается при накоплении первых значений.
        slots = max(8, self._data.maxlen or len(vals) or 1)
        visible = vals[-slots:]
        start_slot = slots - len(visible)
        step = chart.width() / max(1, slots - 1)
        pts = []
        for i, v in enumerate(visible):
            slot = start_slot + i
            x = chart.left() + step * slot
            y = chart.bottom() - chart.height() * ((v - vmin) / span)
            pts.append(QPointF(x, y))
        return pts

    def _draw_line(self, p: QPainter, pts: list[QPointF], chart: QRectF, smooth: bool):
        path = self._smooth_path(pts) if smooth else QPainterPath(pts[0])
        if not smooth:
            for pt in pts[1:]:
                path.lineTo(pt)

        fill = QPainterPath(path)
        fill.lineTo(pts[-1].x(), chart.bottom())
        fill.lineTo(pts[0].x(), chart.bottom())
        fill.closeSubpath()
        area = QLinearGradient(chart.topLeft(), chart.bottomLeft())
        area.setColorAt(0.0, self._c(theme.ACCENT3, 80))
        area.setColorAt(0.55, self._c(theme.ACCENT, 25))
        area.setColorAt(1.0, self._c(theme.ACCENT3, 0))
        p.fillPath(fill, area)

        glow = QLinearGradient(chart.left(), 0, chart.right(), 0)
        glow.setColorAt(0, self._c(theme.ACCENT3, 80))
        glow.setColorAt(1, self._c(theme.ACCENT, 80))
        gp = QPen(glow, 5)
        gp.setCapStyle(Qt.RoundCap)
        gp.setJoinStyle(Qt.RoundJoin)
        p.setPen(gp)
        p.drawPath(path)

        line = QLinearGradient(chart.left(), 0, chart.right(), 0)
        line.setColorAt(0, QColor(theme.ACCENT3))
        line.setColorAt(0.5, QColor(theme.ACCENT2))
        line.setColorAt(1, QColor(theme.ACCENT))
        lp = QPen(line, 2.1)
        lp.setCapStyle(Qt.RoundCap)
        lp.setJoinStyle(Qt.RoundJoin)
        p.setPen(lp)
        p.drawPath(path)

    def _draw_bars(self, p: QPainter, vals: list[float], chart: QRectF, vmin: float, vmax: float):
        avg = self.avg or vals[-1]
        span = (vmax - vmin) or 1.0

        # В биржевом режиме ширина/шаг НЕ зависят от текущего числа точек.
        # Иначе первые 2-3 палочки огромные, потом график «сжимается» по мере
        # накопления данных. Берём фиксированное число слотов = capacity и
        # рисуем значения справа налево: новые приходят справа, старые уходят
        # за левый край — как потоковый market chart.
        slots = max(8, self._data.maxlen or len(vals) or 1)
        visible = vals[-slots:]
        step = chart.width() / slots
        bar_w = max(2.0, min(7.0, step * 0.44))
        baseline = chart.bottom() - chart.height() * ((avg - vmin) / span)

        # Средняя линия.
        p.setPen(QPen(self._c(theme.ACCENT2, 120), 1, Qt.DashLine))
        p.drawLine(QPointF(chart.left(), baseline), QPointF(chart.right(), baseline))

        start_slot = slots - len(visible)
        prev = visible[0]
        for i, v in enumerate(visible):
            slot = start_slot + i
            x = chart.left() + step * slot + step / 2
            y = chart.bottom() - chart.height() * ((v - vmin) / span)
            up = v <= prev  # меньше latency = лучше, поэтому бирюзовый
            color = theme.ACCENT3 if up else theme.PINK
            alpha = 240 if i == len(visible) - 1 else 170
            pen = QPen(self._c(color, alpha), bar_w)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x, baseline), QPointF(x, y))
            prev = v

    def _draw_footer(self, p: QPainter, outer: QRectF, chart: QRectF, vals: list[float]):
        last = vals[-1]
        avg = self.avg or last
        state_color = theme.GREEN if last < 80 else (theme.YELLOW if last < 180 else theme.RED)

        p.setFont(QFont("Consolas", 8, QFont.DemiBold))
        p.setPen(QColor(theme.ACCENT3))
        p.drawText(QRectF(chart.left() + 2, outer.bottom() - 17, 86, 14), Qt.AlignLeft | Qt.AlignVCenter, f"LAST {int(last)}")
        p.setPen(self._c(theme.SUBTEXT, 210))
        p.drawText(QRectF(chart.left() + 88, outer.bottom() - 17, 82, 14), Qt.AlignLeft | Qt.AlignVCenter, f"AVG {int(avg)}")
        p.setPen(self._c(state_color, 235))
        p.drawText(QRectF(chart.right() - 52, outer.bottom() - 17, 50, 14), Qt.AlignRight | Qt.AlignVCenter, "LOW" if last < 80 else ("MID" if last < 180 else "HIGH"))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        w, h = self.width(), self.height()
        if w <= 8 or h <= 8:
            p.end()
            return

        outer = QRectF(1, 1, w - 2, h - 2)
        chart = outer.adjusted(8, 8, -8, -18)
        p.setPen(QPen(self._c(theme.BORDER, 120), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(outer, 10, 10)

        vals = list(self._data)
        if len(vals) < 2:
            self._draw_grid(p, chart, 0, 100)
            p.setPen(self._c(theme.MUTED, 210))
            p.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
            p.drawText(chart, Qt.AlignCenter, "ожидание данных")
            p.end()
            return

        vmin, vmax = self._bounds(vals)
        self._draw_grid(p, chart, vmin, vmax)
        pts = self._points(vals, chart, vmin, vmax)

        if self._mode == "bars":
            self._draw_bars(p, vals, chart, vmin, vmax)
        else:
            self._draw_line(p, pts, chart, smooth=(self._mode == "smooth"))
            last_pt = pts[-1]
            p.setPen(Qt.NoPen)
            p.setBrush(self._c(theme.ACCENT3, 45))
            p.drawEllipse(last_pt, 8, 8)
            p.setBrush(QColor(theme.ACCENT3))
            p.drawEllipse(last_pt, 3.5, 3.5)

        self._draw_footer(p, outer, chart, vals)
        p.end()
