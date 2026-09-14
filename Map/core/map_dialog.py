"""
UmbraNet — Cyber-Map: живая карта реального трафика.
====================================================

Диалог «Кибер-карта» из раздела «Сеть и диагностика».

ВНЕШНИЙ ВИД («военный радар»):
  чёрный фон • материки (Natural Earth, public domain) залиты мелкой
  зелёной клеткой, контур — толстой зелёной линией • границы крупнейших
  стран (РФ, США, КНР, Канада, …) — тоньше и тусклее • мегареки (Нил,
  Амазонка, Миссисипи, …) — приглушённо-голубые • едва заметные подписи
  материков • «мы» — жёлтая точка • цели — красная точка с перекрестием ✕ •
  между нами и целью — тонкий красный пунктир, который «бежит» в сторону
  сервера и через несколько секунд тает. Озёра и мелкие реки не рисуются.

ИСТОРИЯ ПОЧИНКИ (почему карта раньше не работала):

  1. Прежний world.svg был ОДНИМ <path> на ~50 000 точек. Qt SVG молча
     отбрасывает данные пути длиннее ~32 768 точек (QTBUG-120653) —
     карта рисовалась пустым чёрным прямоугольником. SVG пересобран:
     5 путей по <=24 000 координат (с запасом под лимит) + читаемые стили.

  2. Координаты целей были захардкожены пикселями «на глаз». Теперь честная
     проекция lat/lon → сцена (equirectangular, центр 0°) — сверено по
     контрольным точкам континентов.

  3. «Трафик» был рандомом из 7 сайтов. Теперь подписка на реальный QueryLog
     ядра: на карте появляются домены, реально зарезолвленные через
     UmbraNet, с геолокацией IP цели (Map/core/geo.py: офлайн-таблица
     популярных сетей + онлайн ip-api.com с кэшем).

КАК РИСУЮТСЯ МАТЕРИКИ:
  Qt SVG не поддерживает SVG-паттерны (клетчатую заливку), поэтому геометрия
  читается из world.svg напрямую в QPainterPath (_parse_svg_land: наш файл
  содержит только линейные команды m/l/h/v/z) и рисуется QGraphicsPathItem-ом:
  кисть с текстурой «мелкая зелёная клетка» + зелёный контур. Если SVG вдруг
  заменят на файл с кривыми — парсер откажется, и карта откатится на обычный
  рендер через QSvgRenderer (без клетки, но рабочая).

Поток данных:
    DNS-поток ядра → QueryLog.add() → callback (DNS-поток)
    → signal _entry_arrived (UI-поток) → фильтры/дедуп → geo.geolocate_async()
    → callback (worker-поток) → signal _geo_result (UI-поток) → луч на карте.

Все анимации (пульс-кольца, «бегущий» пунктир лучей, затухание) живут в
UI-потоке и гарантированно останавливаются в closeEvent.
"""

from __future__ import annotations

import datetime
import os
import re
import time

from PySide6.QtCore import (
    QAbstractAnimation,
    QEvent,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsOpacityEffect,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from Map.core import geo
from umbranet import theme
from umbranet.engine_adapter import get_query_log
from umbranet.widgets.live_resize import LiveResizeFreezer

# Источники QueryLog, которые не являются «походом на сервер».
# Дублируем строки из core/dns/query_log.py, чтобы не тянуть импорт ядра:
# карта должна открываться даже когда ядро недоступно.
_SKIP_SOURCES = {"bg-refresh", "bogus-NX", "blocked"}
# Для лучей интересны только адресные запросы (превращаются в IP цели)
_MAP_QTYPES = {"A", "AAAA"}

# Антифлуд: не чаще одного луча в MIN_BEAM_INTERVAL секунд,
# один и тот же домен — не чаще DOMAIN_COOLDOWN секунд.
MIN_BEAM_INTERVAL = 1.2
DOMAIN_COOLDOWN = 8.0
# Сколько ждём онлайн-геолокацию, прежде чем признать «локация неизвестна»
GEO_WATCHDOG_MS = 6500

# Палитра карты («военный радар»: чёрный фон, зелёные материки клеткой,
# жёлтый «дом», красные цели и красные тонкие пунктирные лучи)
C_HOME = QColor("#ffd93b")        # «мы» — жёлтый (фиолетовый был плохо виден)
C_TARGET = QColor("#ff4757")      # цель — красный
C_BEAM = QColor("#ff4757")        # луч — красный тонкий пунктир
C_LAND_STROKE = QColor("#2ee06a") # контур материков — зелёный, потолще
C_BORDER = QColor(46, 224, 106, 150)  # границы крупных стран — тоньше/тусклее
C_RIVER = QColor(91, 168, 201, 185)   # мегареки — приглушённо-голубые
C_LABEL = QColor(46, 224, 106, 85)    # подписи материков — едва заметные
C_MAP_BG = "#000000"              # фон карты — чистый чёрный

# Подписи материков: (текст, lon, lat)
_CONTINENT_LABELS = (
    ("ЕВРОПА", 16.0, 51.5),
    ("АЗИЯ", 90.0, 48.0),
    ("АФРИКА", 17.0, 5.0),
    ("СЕВ. АМЕРИКА", -101.0, 45.0),
    ("ЮЖ. АМЕРИКА", -59.5, -14.0),
    ("АВСТРАЛИЯ", 134.0, -25.5),
    ("АНТАРКТИДА", 0.0, -77.0),
)

# Москва по умолчанию (перекрывается config: map_home_lat / map_home_lon)
DEFAULT_HOME = (55.75, 37.62)


# ── Парсер геометрии world.svg → QPainterPath ────────────────────────────────

_NUM_TOKEN = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TOKEN_RE = re.compile(rf"[MmLlHhVvZz]|{_NUM_TOKEN}")
_CMD_CHARS = set("MmLlHhVvZz")
_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*"[\s]*([-\d.]+)[\s]+([-\d.]+)[\s]+([\d.]+)[\s]+([\d.]+)"')
_PATH_TAG_RE = re.compile(r"<path\b([^>]*)>", re.S)

# Слои карты (class-атрибуты в world.svg, см. tools/generate_world_map.py)
_LAYERS = ("land", "border", "river")


def _tag_attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', attrs)
    return m.group(1) if m else None


def _append_path_data(path: QPainterPath, d: str) -> None:
    """Дописывает в QPainterPath данные одного SVG-пути.

    Понимает ТОЛЬКО линейные команды (m/l/h/v/z, в любом регистре) — наш
    world.svg состоит только из них. Кривые/арки → ValueError → вызывающий
    код откатится на QSvgRenderer.
    """
    # Строгая предвалидация: строка (без пробелов/запятых) должна целиком
    # состоять из разрешённых токенов. Иначе findall молча «проглотит»
    # посторонний символ (например, C из кубической кривой) и исказит путь.
    stripped = re.sub(r"[\s,]+", "", d)
    if not re.fullmatch(rf"(?:[MmLlHhVvZz]|{_NUM_TOKEN})+", stripped):
        raise ValueError("unsupported commands/characters in path data")

    tokens = _TOKEN_RE.findall(d)
    i, n = 0, len(tokens)
    cx = cy = 0.0          # текущая точка (абсолютная)
    sx = sy = 0.0          # старт текущего subpath
    has_current = False

    def take(count: int) -> list[float]:
        nonlocal i
        vals = []
        for _ in range(count):
            if i >= n or tokens[i] in _CMD_CHARS:
                raise ValueError("unexpected end of path data")
            vals.append(float(tokens[i]))
            i += 1
        return vals

    while i < n:
        cmd = tokens[i]
        i += 1
        if cmd in "Zz":
            if has_current:
                path.closeSubpath()
                cx, cy = sx, sy
                has_current = False
            continue
        if cmd in "Mm":
            rel = cmd == "m"
            first = True
            while i < n and tokens[i] not in _CMD_CHARS:
                x, y = take(2)
                if rel:
                    x += cx
                    y += cy
                cx, cy = x, y
                if first:            # первая пара после m — moveto,
                    path.moveTo(x, y) # остальные — неявные lineto
                    sx, sy = x, y
                    has_current = True
                    first = False
                else:
                    path.lineTo(x, y)
            continue
        if cmd in "Ll":
            rel = cmd == "l"
            while i < n and tokens[i] not in _CMD_CHARS:
                x, y = take(2)
                if rel:
                    x += cx
                    y += cy
                cx, cy = x, y
                path.lineTo(x, y)
            continue
        if cmd in "Hh":
            rel = cmd == "h"
            while i < n and tokens[i] not in _CMD_CHARS:
                (x,) = take(1)
                cx = cx + x if rel else x
                path.lineTo(cx, cy)
            continue
        if cmd in "Vv":
            rel = cmd == "v"
            while i < n and tokens[i] not in _CMD_CHARS:
                (y,) = take(1)
                cy = cy + y if rel else y
                path.lineTo(cx, cy)
            continue
        raise ValueError(f"unsupported path command: {cmd!r}")


def _parse_map_svg(svg_path: str):
    """Читает world.svg и возвращает (слои, vb_w, vb_h) или None.

    Слои — dict: {"land": QPainterPath, "border": QPainterPath,
    "river": QPainterPath} по class-атрибутам <path>. class="land"
    обязателен; border/river могут отсутствовать (пустые пути).

    None = файл не разобрался (нет файла, кривые команды, битый viewBox) —
    тогда карта рисуется через QSvgRenderer как раньше.
    """
    try:
        with open(svg_path, "r", encoding="utf-8") as f:
            text = f.read()
        vb = _VIEWBOX_RE.search(text)
        if vb is None:
            return None
        vb_w = max(1.0, float(vb.group(3)))
        vb_h = max(1.0, float(vb.group(4)))

        layers = {name: QPainterPath() for name in _LAYERS}
        seen_land = False
        for m in _PATH_TAG_RE.finditer(text):
            attrs = m.group(1)
            d = _tag_attr(attrs, "d")
            if not d:
                continue
            cls = (_tag_attr(attrs, "class") or "land").strip()
            if cls not in layers:
                cls = "land"
            # SVG по умолчанию использует fill-rule=nonzero. У QPainterPath
            # по умолчанию OddEvenFill — из-за него накладывающиеся субпути
            # дают «дырки». WindingFill = эквивалент nonzero.
            layers[cls].setFillRule(Qt.WindingFill)
            _append_path_data(layers[cls], d)
            if cls == "land":
                seen_land = True

        if not seen_land or layers["land"].isEmpty():
            return None
        return layers, vb_w, vb_h
    except Exception:
        return None


def _make_grid_texture(tile: int = 7) -> QPixmap:
    """Текстура «мелкая зелёная клетка» для заливки материков.

    Тайл: тёмно-зелёная подложка + две зелёные линии (правая и нижняя
    грань клетки). При бесконечном тайлинге получается мелкая сетка.
    """
    base = QColor(8, 28, 16)            # подложка континентов
    line = QColor(46, 224, 106, 95)     # линии клетки (зелёные, ~37%)
    pm = QPixmap(tile, tile)
    pm.fill(base)
    p = QPainter(pm)
    p.setPen(QPen(line, 1))
    p.drawLine(0, tile - 1, tile - 1, tile - 1)   # низ клетки
    p.drawLine(tile - 1, 0, tile - 1, tile - 1)   # правая грань клетки
    p.end()
    return pm


class _PulseRing(QGraphicsEllipseItem):
    """Расходящееся кольцо: живёт ``waves`` повторов (или вечно) и
    самоудаляется со сцены по завершении."""

    def __init__(self, pos: QPointF, color: QColor, diameter: float = 34.0,
                 duration_ms: int = 1500, waves: int = 3, forever: bool = False):
        super().__init__()
        self._cx, self._cy = pos.x(), pos.y()
        self._d0, self._d1 = 5.0, float(diameter)

        pen = QPen(color)
        pen.setWidthF(1.7)
        self.setPen(pen)
        self.setBrush(Qt.NoBrush)
        self.setZValue(6)

        self.anim = QVariantAnimation()
        self.anim.setDuration(duration_ms)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setLoopCount(-1 if forever else max(1, waves))
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self._die)

    def start(self):
        self.anim.start()

    def stop_now(self):
        """Мягкая остановка без удаления (используется при закрытии диалога)."""
        self.anim.stop()

    def _tick(self, value):
        if self.scene() is None:
            self.anim.stop()
            return
        phase = float(value) % 1.0
        d = self._d0 + (self._d1 - self._d0) * phase
        self.setRect(QRectF(self._cx - d / 2, self._cy - d / 2, d, d))
        self.setOpacity(1.0 - phase)

    def _die(self):
        scene = self.scene()
        if scene is not None:
            scene.removeItem(self)
        self.anim.stop()


class _Beam:
    """Луч «дом → цель»: тонкий красный пунктир, который «бежит» к серверу
    и через несколько секунд тает.

    Вся жизнь луча — ОДНА QVariantAnimation (без отложенных QTimer):
      [0 .. FLOW_MS)               — пунктир «бежит» (сдвиг фазы штрихов)
      [FLOW_MS .. FLOW_MS+FADE_MS) — плавное затухание
      конец                        — луч удаляется со сцены
    Анимация запарентена на диалог: он владеет ею, луч лишь держит ссылку.
    Никаких singleShot: после closeEvent объект луча может быть собран GC,
    и отложенный вызов его метода падал бы на «зомби»-обёртке.
    """

    FLOW_MS = 5200   # сколько времени «бежит» пунктир
    FADE_MS = 900    # сколько затухает
    DASH_LEN = 11.0  # длина паттерна штрихов (6+5)
    DASH_SPEED = DASH_LEN / 600.0  # единиц паттерна в мс (11 за 600мс)

    def __init__(self, scene: QGraphicsScene, p1: QPointF, p2: QPointF, dialog):
        self.scene = scene
        self.items: list[QGraphicsItem] = []
        self._dead = False

        # 1) едва заметный красный ореол — чтобы пунктир читался на чёрном
        halo = scene.addLine(p1.x(), p1.y(), p2.x(), p2.y(),
                             QPen(QColor(255, 71, 87, 40), 3.2, Qt.SolidLine))
        halo.setZValue(3)
        self.items.append(halo)

        # 2) основной тонкий красный пунктир
        pen = QPen(C_BEAM, 1.3, Qt.DashLine)
        pen.setDashPattern([6, 5])
        self.line = scene.addLine(p1.x(), p1.y(), p2.x(), p2.y(), pen)
        self.line.setZValue(4)
        self.items.append(self.line)

        # 3) единая анимация: течение → затухание → самоудаление
        self._fade_effect = QGraphicsOpacityEffect()
        self.line.setGraphicsEffect(self._fade_effect)
        self.anim = QVariantAnimation(dialog)
        self.anim.setDuration(self.FLOW_MS + self.FADE_MS)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(float(self.FLOW_MS + self.FADE_MS))
        self.anim.valueChanged.connect(self._on_anim)
        self.anim.finished.connect(self._die)
        self.anim.start()

    def _on_anim(self, t: float):
        if self._dead:
            return
        if t < self.FLOW_MS:
            # фаза «течения»: сдвигаем штрихи вдоль линии
            pen = self.line.pen()
            pen.setDashOffset(-((t * self.DASH_SPEED) % self.DASH_LEN))
            self.line.setPen(pen)
        else:
            # фаза затухания
            self._fade_effect.setOpacity(
                max(0.0, 1.0 - (t - self.FLOW_MS) / self.FADE_MS))

    def stop(self):
        """Аварийная остановка (закрытие диалога / вытеснение из списка)."""
        if self._dead:
            return
        self._dead = True
        self.anim.stop()
        self._remove_items()

    def _die(self):
        self._dead = True
        self._remove_items()

    def _remove_items(self):
        for item in self.items:
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
        self.items.clear()


class CyberMapDialog(QDialog):
    """Живой радар: реальные DNS-запросы UmbraNet на карте мира."""

    # Записи из DNS-потока → UI-поток
    _entry_arrived = Signal(object)
    # Результат геолокации из worker-потока → UI-поток
    _geo_result = Signal(object)

    MAP_W = 880
    MAP_H = 440

    def __init__(self, parent=None, engine=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("Cyber-Map (реальный трафик UmbraNet)")
        self.setMinimumSize(940, 600)
        self.setStyleSheet(f"QDialog {{ background: {theme.BG}; }}")

        # ── статистика для статусной строки ──
        self._stat_beams = 0
        self._stat_domains: set[str] = set()
        self._stat_geo_hits = 0
        self._stat_geo_miss = 0
        self._last_beam_at = 0.0          # time.monotonic()
        self._recent_domains: dict[str, float] = {}
        self._beams: list[_Beam] = []
        self._pulses: list[_PulseRing] = []
        self._watchdogs: list[QTimer] = []
        self._target_dots: list[QGraphicsEllipseItem] = []
        self._closing = False

        self._build_ui()

        # ── подписка на реальный трафик ──
        self._entry_arrived.connect(self._handle_entry)
        self._geo_result.connect(self._handle_geo)
        self._qlog = get_query_log()
        if self._qlog is not None:
            try:
                self._qlog.subscribe(self._on_entry_from_core)
            except Exception:
                self._qlog = None

        # Статус-таймер: обновляет «движок остановлен/слушаю запросы»
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1200)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()
        self._refresh_status()

        # Пауза анимаций при перетаскивании окна: пока диалог тащат,
        # сцена не перерисовывается 30+ раз в секунду и окно движется
        # нативно-плавно (иначе на ноутбуках перетаскивание — слайд-шоу).
        self._move_resume_timer = QTimer(self)
        self._move_resume_timer.setSingleShot(True)
        self._move_resume_timer.setInterval(250)
        self._move_resume_timer.timeout.connect(self._resume_animations)

        # Заморозка контента при живом resize: снимок окна + оверлей,
        # тяжёлая сцена не перерисовывается на каждый шаг изменения размера.
        self._live_freezer = LiveResizeFreezer(self)
        self._qwin_filter_installed = False   # фильтр на QWindow (ставится в showEvent)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("Живой радар маршрутизации 🛰️")
        title.setStyleSheet(f"color:{theme.WHITE};font-size:22px;font-weight:bold;")
        head.addWidget(title)
        head.addStretch()
        self.lbl_status = QLabel("Инициализация…")
        self.lbl_status.setStyleSheet(f"color:{theme.SUBTEXT};font-size:13px;")
        head.addWidget(self.lbl_status)
        lay.addLayout(head)

        sub = QLabel(
            "Лучи — реальные DNS-запросы, прошедшие через UmbraNet. "
            "Локация цели определяется по IP (офлайн-база + ip-api.com). "
            "Наведите курсор на точку, чтобы увидеть домен."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{theme.MUTED};font-size:11px;background:transparent;border:none;")
        lay.addWidget(sub)

        self.view = QGraphicsView()
        self.scene = QGraphicsScene()
        self.scene.setSceneRect(0, 0, self.MAP_W, self.MAP_H)
        self.view.setScene(self.scene)
        self.view.setStyleSheet(
            f"QGraphicsView {{ border: none; background: {C_MAP_BG}; border-radius: 10px; }}"
        )
        self.view.setRenderHint(QPainter.Antialiasing)
        lay.addWidget(self.view, 1)

        self._draw_map()

        # ── журнал событий ──
        self.log_table = QTableWidget(0, 4)
        self.log_table.setHorizontalHeaderLabels(
            ["Время", "Домен", "IP цели", "Локация"])
        self.log_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.setFixedHeight(140)
        self.log_table.setStyleSheet(
            f"QTableWidget {{ background: {theme.INPUT_BG}; color: {theme.TEXT};"
            " border: none; font-size: 12px; }"
            f"QHeaderView::section {{ background: {theme.CARD}; color: {theme.SUBTEXT};"
            " border: none; padding: 4px; font-weight: bold; }"
        )
        lay.addWidget(self.log_table)

    # ── карта и проекция ─────────────────────────────────────────────────────

    def _draw_map(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        svg_path = os.path.join(base_dir, "Map", "assets", "world.svg")

        # Параметры размещения карты внутри сцены (нужны для проекции)
        self._map_native_w = float(self.MAP_W)
        self._map_native_h = float(self.MAP_H)
        self._map_scale = 1.0
        self._map_off_x = 0.0
        self._map_off_y = 0.0
        self._map_loaded = False

        parsed = _parse_map_svg(svg_path)
        if parsed is not None:
            layers, vw, vh = parsed

            # Вписываем карту в сцену целиком (equirectangular 2:1).
            scale = min(self.MAP_W / vw, self.MAP_H / vh)
            off_x = (self.MAP_W - vw * scale) / 2.0
            off_y = (self.MAP_H - vh * scale) / 2.0
            # Переводим геометрию из координат SVG в пиксели сцены,
            # чтобы пер/текстура жили в экранных пикселях (не мылились).
            to_scene = QTransform(scale, 0.0, 0.0, scale, off_x, off_y)

            def add_layer(name: str, brush, pen, z: float) -> None:
                item = QGraphicsPathItem(to_scene.map(layers[name]))
                item.setBrush(brush)
                item.setPen(pen)
                item.setZValue(z)
                # Слои статичны: кэшируем отрисовку, чтобы постоянные
                # анимации (кольца/лучи) не перерисовывали их каждый кадр.
                item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
                self.scene.addItem(item)

            # 1) материки: мелкая зелёная клетка + контур потолще
            land_pen = QPen(C_LAND_STROKE)
            land_pen.setWidthF(1.7)
            land_pen.setJoinStyle(Qt.RoundJoin)
            add_layer("land", QBrush(_make_grid_texture()), land_pen, -10)

            # 2) границы крупных стран (РФ, США, КНР, Канада, …) — тоньше
            if not layers["border"].isEmpty():
                border_pen = QPen(C_BORDER)
                border_pen.setWidthF(1.1)
                border_pen.setJoinStyle(Qt.RoundJoin)
                add_layer("border", Qt.NoBrush, border_pen, -9)

            # 3) мегареки (Нил, Амазонка, …) — приглушённо-голубые
            if not layers["river"].isEmpty():
                river_pen = QPen(C_RIVER)
                river_pen.setWidthF(1.0)
                river_pen.setCapStyle(Qt.RoundCap)
                add_layer("river", Qt.NoBrush, river_pen, -8)

            self._map_native_w, self._map_native_h = vw, vh
            self._map_scale = scale
            self._map_off_x, self._map_off_y = off_x, off_y
            self._map_loaded = True
        else:
            # Фолбэк: рисуем world.svg как обычный SVG (без клетки).
            # ВАЖНО: рендерер обязан жить пока жив диалог. setSharedRenderer()
            # НЕ забирает владение: локальная переменная → Python удалит
            # C++-объект после выхода из _draw_map, и первый paint сцены
            # уронит приложение (висячий указатель).
            self._svg_renderer = QSvgRenderer(svg_path)
            renderer = self._svg_renderer
            if os.path.exists(svg_path) and renderer.isValid():
                native = renderer.defaultSize()
                nw = max(1.0, float(native.width()))
                nh = max(1.0, float(native.height()))

                scale = min(self.MAP_W / nw, self.MAP_H / nh)
                self._map_native_w, self._map_native_h = nw, nh
                self._map_scale = scale
                self._map_off_x = (self.MAP_W - nw * scale) / 2.0
                self._map_off_y = (self.MAP_H - nh * scale) / 2.0

                map_item = QGraphicsSvgItem()
                map_item.setSharedRenderer(renderer)
                map_item.setScale(scale)
                map_item.setPos(self._map_off_x, self._map_off_y)
                map_item.setZValue(-10)
                map_item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
                self.scene.addItem(map_item)
                self._map_loaded = True
            else:
                self.lbl_status.setText("⚠ world.svg не найден/повреждён — карта без материков")

        self._add_continent_labels()
        self._place_home()

    def _add_continent_labels(self):
        """Едва заметные подписи материков — для читаемости карты."""
        font = QFont()
        font.setBold(True)
        font.setPointSize(8)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        for name, lon, lat in _CONTINENT_LABELS:
            item = QGraphicsSimpleTextItem(name)
            item.setFont(font)
            item.setBrush(QBrush(C_LABEL))
            item.setZValue(-7)
            pos = self._lonlat_to_scene(lon, lat)
            br = item.boundingRect()
            item.setPos(pos.x() - br.width() / 2.0, pos.y() - br.height() / 2.0)
            self.scene.addItem(item)

    def _lonlat_to_scene(self, lon: float, lat: float) -> QPointF:
        """Equirectangular: lon [-180..180], lat [90..-90] → координаты сцены."""
        x = self._map_off_x + (lon + 180.0) / 360.0 * self._map_native_w * self._map_scale
        y = self._map_off_y + (90.0 - lat) / 180.0 * self._map_native_h * self._map_scale
        return QPointF(x, y)

    def _home_lonlat(self) -> tuple[float, float]:
        cfg = getattr(self.engine, "config", None) or {}
        try:
            lat = float(cfg.get("map_home_lat", DEFAULT_HOME[0]))
            lon = float(cfg.get("map_home_lon", DEFAULT_HOME[1]))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return (lat, lon)
        except (TypeError, ValueError):
            pass
        return DEFAULT_HOME

    def _place_home(self):
        lat, lon = self._home_lonlat()
        self.home_pos = self._lonlat_to_scene(lon, lat)

        dot = QGraphicsEllipseItem(self.home_pos.x() - 5, self.home_pos.y() - 5, 10, 10)
        dot.setBrush(QBrush(C_HOME))
        dot.setPen(QPen(Qt.NoPen))
        dot.setZValue(7)
        dot.setToolTip("Ваше расположение (конфиг: map_home_lat / map_home_lon)")
        self.scene.addItem(dot)

        # Постоянное «сердцебиение» дома: бесконечная пульсация.
        home_ring = _PulseRing(self.home_pos, C_HOME, diameter=40,
                               duration_ms=1600, forever=True)
        self.scene.addItem(home_ring)
        self._pulses.append(home_ring)
        home_ring.start()

    # ── приём трафика ────────────────────────────────────────────────────────

    def _on_entry_from_core(self, entry):
        """Вызывается из DNS-потока — только эмитим сигнал."""
        if not self._closing:
            self._entry_arrived.emit(entry)

    def _handle_entry(self, entry):
        """UI-поток: фильтрация, дедуп, запуск геолокации."""
        if entry is None or self._closing:
            return

        source = str(getattr(entry, "source", "") or "")
        if source in _SKIP_SOURCES:
            return
        qtype = str(getattr(entry, "qtype", "") or "").upper()
        if qtype not in _MAP_QTYPES:
            return
        rcode = str(getattr(entry, "rcode", "NOERROR") or "").upper()
        if rcode not in ("NOERROR", ""):
            return

        domain = str(getattr(entry, "domain", "") or "").strip().rstrip(".")
        if not domain:
            return

        now = time.monotonic()

        # Дедуп: тот же домен в течение DOMAIN_COOLDOWN — пропускаем.
        last = self._recent_domains.get(domain)
        if last is not None and now - last < DOMAIN_COOLDOWN:
            return
        self._recent_domains[domain] = now
        if len(self._recent_domains) > 512:
            cutoff = now - DOMAIN_COOLDOWN
            self._recent_domains = {
                d: t for d, t in self._recent_domains.items() if t > cutoff
            }

        # Антифлуд: не чаще одного луча в MIN_BEAM_INTERVAL секунд.
        allow_beam = (now - self._last_beam_at) >= MIN_BEAM_INTERVAL
        if allow_beam:
            self._last_beam_at = now

        # IP цели: первый глобальный адрес из ответов.
        # fake-IP (заглушки DPI-движка) геолокировать бессмысленно.
        answers = list(getattr(entry, "answers", None) or [])
        target_ip = ""
        note = str(getattr(entry, "note", "") or "").lower()
        if "fake" not in note:
            for ans in answers:
                ans = str(ans).strip()
                if geo.is_public_ip(ans):
                    target_ip = ans
                    break

        payload = {
            "domain": domain,
            "ip": target_ip,
            "allow_beam": allow_beam,
            "ts": time.time(),
        }

        def _geo_cb(lat, lon, label):
            payload["lat"] = lat
            payload["lon"] = lon
            payload["label"] = label
            self._geo_result.emit(payload)

        if target_ip:
            # Мгновенный ответ (кэш/офлайн-база) придёт синхронно, сетевой —
            # из worker-потока. Ставим сторож на случай «сети нет».
            # QTimer-инстанс (а не singleShot): запарентен на диалог и
            # гарантированно глушится в closeEvent — никаких вызовов
            # методов уже собранного GC объекта.
            geo.geolocate_async(target_ip, _geo_cb)
            if "lat" not in payload:
                watchdog = QTimer(self)
                watchdog.setSingleShot(True)
                watchdog.timeout.connect(
                    lambda p=payload: self._geo_timeout(p))
                watchdog.start(GEO_WATCHDOG_MS)
                self._watchdogs.append(watchdog)
        else:
            # IP нет (fake-IP движка или пустые ответы) — пробуем по домену.
            dom_geo = geo.domain_fallback(domain)
            if dom_geo is not None:
                payload["lat"] = dom_geo[0]
                payload["lon"] = dom_geo[1]
                payload["label"] = dom_geo[2]
                self._geo_result.emit(payload)
            else:
                self._geo_result.emit(payload)

    def _geo_timeout(self, payload: dict):
        """Сторож: гео не пришло вовремя (нет сети) — всё равно логируем."""
        if payload.get("done") or self._closing:
            return
        payload["done"] = True
        self._watchdogs = [t for t in self._watchdogs if t.isActive()]
        self._stat_geo_miss += 1
        self._stat_domains.add(payload.get("domain", ""))
        self._add_log_row(payload, located=False)
        self._refresh_status()

    def _handle_geo(self, payload: dict):
        """UI-поток: рисуем луч и пишем строку в журнал."""
        if payload.get("done") or self._closing:
            return
        payload["done"] = True
        self._watchdogs = [t for t in self._watchdogs if t.isActive()]

        domain = payload.get("domain", "")
        lat, lon = payload.get("lat"), payload.get("lon")
        label = str(payload.get("label", "") or "")

        self._stat_domains.add(domain)

        if lat is None or lon is None:
            self._stat_geo_miss += 1
            self._add_log_row(payload, located=False)
            self._refresh_status()
            return

        self._stat_geo_hits += 1

        # Рисуем луч; любая ошибка отрисовки не должна съедать строку журнала.
        if payload.get("allow_beam", True):
            try:
                self._draw_beam(domain, self._lonlat_to_scene(float(lon), float(lat)))
                self._stat_beams += 1
            except Exception:
                pass

        self._add_log_row(payload, located=True, label=label)
        self._refresh_status()

    # ── рисование ────────────────────────────────────────────────────────────

    def _draw_beam(self, domain: str, target_pos: QPointF):
        # ── маркер цели: красная точка + перекрестие ✕ («цель захвачена») ──
        marker_items: list[QGraphicsItem] = []

        dot = QGraphicsEllipseItem(target_pos.x() - 3.5, target_pos.y() - 3.5, 7, 7)
        dot.setBrush(QBrush(C_TARGET))
        dot.setPen(QPen(Qt.NoPen))
        dot.setZValue(7)
        dot.setToolTip(domain)
        self.scene.addItem(dot)
        marker_items.append(dot)

        cross_pen = QPen(QColor(255, 71, 87, 210))
        cross_pen.setWidthF(1.2)
        r = 8.0
        for x1, y1, x2, y2 in (
            (target_pos.x() - r, target_pos.y() - r, target_pos.x() + r, target_pos.y() + r),
            (target_pos.x() - r, target_pos.y() + r, target_pos.x() + r, target_pos.y() - r),
        ):
            cross = self.scene.addLine(x1, y1, x2, y2, cross_pen)
            cross.setZValue(7)
            cross.setToolTip(domain)
            marker_items.append(cross)

        self._target_dots.extend(marker_items)
        while len(self._target_dots) > 30:
            old = self._target_dots.pop(0)
            scene = old.scene()
            if scene is not None:
                scene.removeItem(old)

        ring = _PulseRing(target_pos, C_TARGET, diameter=30, duration_ms=1300, waves=3)
        self.scene.addItem(ring)
        self._pulses.append(ring)
        ring.start()

        # Луч дом → цель
        beam = _Beam(self.scene, self.home_pos, target_pos, self)
        self._beams.append(beam)
        while len(self._beams) > 8:
            old = self._beams.pop(0)
            old.stop()

    def _add_log_row(self, payload: dict, located: bool, label: str = ""):
        table = self.log_table
        row = table.rowCount()
        table.insertRow(row)

        time_str = datetime.datetime.fromtimestamp(
            float(payload.get("ts") or time.time())).strftime("%H:%M:%S")
        table.setItem(row, 0, QTableWidgetItem(time_str))

        dom_item = QTableWidgetItem(str(payload.get("domain", "?")))
        dom_item.setForeground(QBrush(QColor(theme.ACCENT)))
        table.setItem(row, 1, dom_item)

        table.setItem(row, 2, QTableWidgetItem(str(payload.get("ip") or "—")))
        table.setItem(row, 3, QTableWidgetItem(
            label if located else "локация неизвестна"))

        # Журнал не растёт бесконечно
        if row > 200:
            table.removeRow(0)

        table.scrollToBottom()

    # ── статус ───────────────────────────────────────────────────────────────

    def _refresh_status(self):
        if not self._map_loaded:
            return
        running = bool(getattr(self.engine, "running", False))
        if not running:
            self.lbl_status.setText(
                "UmbraNet остановлен — запустите движок, чтобы увидеть трафик")
            return
        if self._qlog is None:
            self.lbl_status.setText("Ядро недоступно — журнал запросов не подключён")
            return
        self.lbl_status.setText(
            f"Слушаю DNS-запросы • лучей: {self._stat_beams} • "
            f"доменов: {len(self._stat_domains)} • гео найдено: {self._stat_geo_hits}"
        )

    # ── пауза анимаций при перетаскивании окна ───────────────────────────────

    def moveEvent(self, event):
        """Окно тащат — ставим анимации на паузу, чтобы они не конкурировали
        с drag-loop Windows за перерисовку (лаги перетаскивания)."""
        super().moveEvent(event)
        self._pause_animations_while_geometry_changes()

    def resizeEvent(self, event):
        """Изменение размера окна: анимации на паузу. Заморозку контента
        делает фильтр на windowHandle (eventFilter ниже) — он срабатывает
        ДО пересчёта layout; сюда попадаем только как запасной путь."""
        super().resizeEvent(event)
        if self.isVisible() and not self._closing and not self._qwin_filter_installed:
            if not self._live_freezer.active:
                self._live_freezer.begin()
            else:
                self._live_freezer.step()
        self._pause_animations_while_geometry_changes()

    def showEvent(self, event):
        # Первый показ: фильтр на QWindow — его Resize прилетает от системы
        # раньше, чем Qt пересчитает геометрию виджета и layout.
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._qwin_filter_installed:
            handle.installEventFilter(self)
            self._qwin_filter_installed = True

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Resize
                and obj is self.windowHandle()
                and not self._closing
                and self.isVisible()):
            if not self._live_freezer.active:
                self._live_freezer.begin()
            else:
                self._live_freezer.step()
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        # Диалог спрятали во время resize — обязательно разморозить контент.
        self._live_freezer.end()
        super().hideEvent(event)

    def _pause_animations_while_geometry_changes(self):
        if self._closing:
            return
        self._set_animations_paused(True)
        self._move_resume_timer.start()   # возобновим через 250 мс тишины

    def _resume_animations(self):
        # Размер/позиция устоялись: снимаем заморозку (один relayout) и
        # возвращаем анимации.
        self._live_freezer.end()
        self._set_animations_paused(False)

    def _set_animations_paused(self, paused: bool):
        if self._closing:
            return
        for anim in self._live_animations():
            state = anim.state()
            if paused and state == QAbstractAnimation.State.Running:
                anim.pause()
            elif not paused and state == QAbstractAnimation.State.Paused:
                anim.resume()

    def _live_animations(self):
        for pulse in self._pulses:
            yield pulse.anim
        for beam in self._beams:
            yield beam.anim

    # ── завершение ───────────────────────────────────────────────────────────

    def closeEvent(self, event):
        # Порядок важен: сначала говорим ядру «мы уходим» (чтобы DNS-поток
        # не эмитил в умирающий диалог), потом глушим всю анимацию.
        self._closing = True

        if self._qlog is not None:
            try:
                self._qlog.unsubscribe(self._on_entry_from_core)
            except Exception:
                pass
            self._qlog = None

        self._status_timer.stop()
        self._move_resume_timer.stop()
        self._live_freezer.end()   # на всякий случай: снять заморозку, если resize был прерван закрытием
        for beam in self._beams:
            beam.stop()
        self._beams.clear()
        for pulse in self._pulses:
            pulse.stop_now()
        self._pulses.clear()
        for watchdog in self._watchdogs:
            watchdog.stop()
        self._watchdogs.clear()
        self.scene.clear()   # удаляет оставшиеся items (точки, линии, карту)
        event.accept()

    def reject(self):  # Esc закрывает через тот же путь, что и крестик
        self.close()
