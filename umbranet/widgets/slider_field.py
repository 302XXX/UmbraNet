"""
UmbraNet - поле «слайдер + число» (PySide6).

Горизонтальный ползунок с подписью текущего значения. Можно тянуть мышью.
Сигнал valueChanged(int).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QWidget

from umbranet import theme


class _NoWheelSlider(QSlider):
    """QSlider без изменения значения колесом мыши.

    В настройках пользователь часто прокручивает страницу колесом и случайно
    задевает курсором TTL-слайдер. Стандартный QSlider в Qt меняет значение от
    wheelEvent даже без клика — это опасно для настроек. Оставляем управление
    только перетаскиванием ручки мышью/тачпадом.
    """

    def wheelEvent(self, event):
        event.ignore()


class SliderField(QWidget):
    valueChanged = Signal(int)

    def __init__(self, label: str, value: int, minimum: int, maximum: int,
                 suffix: str = "сек"):
        super().__init__()
        self._suffix = suffix

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        lbl = QLabel(label)
        lbl.setFixedWidth(150)
        lbl.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;background:transparent;border:none;")
        lay.addWidget(lbl)

        self._slider = _NoWheelSlider(Qt.Horizontal)
        self._slider.setMinimum(minimum)
        self._slider.setMaximum(maximum)
        self._slider.setValue(max(minimum, min(maximum, value)))
        self._slider.setCursor(Qt.PointingHandCursor)
        self._slider.setStyleSheet(self._slider_qss())
        self._slider.valueChanged.connect(self._on_change)
        lay.addWidget(self._slider, 1)

        self._value_lbl = QLabel(self._fmt(value))
        self._value_lbl.setFixedWidth(90)
        self._value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._value_lbl.setStyleSheet(
            f"color:{theme.ACCENT3};font-size:13px;font-weight:600;font-family:Consolas;"
            "background:transparent;border:none;")
        lay.addWidget(self._value_lbl)

    def _fmt(self, v: int) -> str:
        return f"{v} {self._suffix}" if self._suffix else str(v)

    def _slider_qss(self) -> str:
        return (
            f"QSlider::groove:horizontal{{height:6px;border-radius:3px;"
            f"background:{theme.INPUT_BG};}}"
            f"QSlider::sub-page:horizontal{{height:6px;border-radius:3px;"
            f"background:{theme.grad(theme.ACCENT, theme.ACCENT2)};}}"
            f"QSlider::handle:horizontal{{width:16px;height:16px;margin:-6px 0;"
            f"border-radius:8px;background:{theme.WHITE};}}"
            f"QSlider::handle:horizontal:hover{{background:{theme.ACCENT3};}}"
        )

    def _on_change(self, v: int):
        self._value_lbl.setText(self._fmt(v))
        self.valueChanged.emit(v)

    def value(self) -> int:
        return self._slider.value()

    def setValue(self, v: int):
        self._slider.blockSignals(True)
        self._slider.setValue(v)
        self._value_lbl.setText(self._fmt(v))
        self._slider.blockSignals(False)
