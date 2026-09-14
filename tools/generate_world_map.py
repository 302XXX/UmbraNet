"""
UmbraNet — генератор Map/assets/world.svg из данных Natural Earth.

Запуск (из корня репо):
    python tools/generate_world_map.py

Что делает:
  1. Берёт три публичных датасета Natural Earth 110m (public domain):
     - ne_110m_land                     — материки/острова (береговые линии)
     - ne_110m_admin_0_countries        — границы государств (нужны только
                                          крупнейшие страны: РФ, США, КНР…)
     - ne_110m_rivers_lake_centerlines  — только мегареки (Нил, Амазонка…)
     Если файлов нет рядом — скачивает их с GitHub (nvkelso/natural-earth-vector).
  2. Конвертирует в SVG с прямоугольной (equirectangular) проекцией:
     viewBox "0 0 360 180", x = lon + 180, y = 90 - lat.
  3. Раскладывает по слоям через class-атрибуты:
     - class="land"   — полигоны суши (заливка + контур)
     - class="border" — границы крупных стран (только линия)
     - class="river"  — крупные реки (только линия)
     Озёра и мелкие реки НЕ включаются вовсе.

ИНВАРИАНТЫ (нарушать нельзя — сломается рендер карты в Map/core/map_dialog.py):
  * Только ЛИНЕЙНЫЕ команды пути: M/L/Z (парсер карты не умеет кривые;
    увидит кривые — молча откатится на QSvgRenderer без клетчатой заливки).
  * Не более 24 000 чисел на один <path>: Qt SVG отбрасывает пути длиннее
    ~32 768 точек (QTBUG-120653). Скрипт сам режет по чанкам.
  * Кольца-«дырки» (озёра-гиганты типа Каспия) наматываются противоположно
    внешнему кольцу — иначе fill-rule nonzero их не вырежет.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE_DIR, "Map", "assets", "world.svg")
DATA_DIR = os.path.join(BASE_DIR, "tools", "ne_data")

SOURCES = {
    "ne_110m_land.geojson":
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson",
    "ne_110m_admin_0_countries.geojson":
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson",
    "ne_110m_rivers_lake_centerlines.geojson":
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_rivers_lake_centerlines.geojson",
}

# Крупнейшие страны (площадь >= ~1.5 млн км²), чьи границы рисуем.
BIG_COUNTRIES_ISO3 = {
    "RUS", "CAN", "USA", "CHN", "BRA", "AUS", "IND", "ARG", "KAZ", "DZA",
    "COD", "SAU", "MEX", "IDN", "SDN", "LBY", "IRN", "MNG",
}

MAX_NUMBERS_PER_PATH = 24000   # запас под лимит Qt SVG (~32k точек на путь)


# ── загрузка данных ──────────────────────────────────────────────────────────

def load_geojson(name: str) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        url = SOURCES[name]
        print(f"скачиваю {url} …")
        urllib.request.urlretrieve(url, path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── геометрия ────────────────────────────────────────────────────────────────

def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _xy(lon: float, lat: float) -> tuple[float, float]:
    """Equirectangular → координаты viewBox 0..360 / 0..180."""
    return lon + 180.0, 90.0 - lat


def _dedupe(ring: list) -> list:
    out = []
    for p in ring:
        if not out or p[0] != out[-1][0] or p[1] != out[-1][1]:
            out.append(p)
    while len(out) > 1 and out[0][0] == out[-1][0] and out[0][1] == out[-1][1]:
        out.pop()
    return out


def _signed_area(ring: list) -> float:
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def ring_d(ring: list, close: bool, ccw: bool | None = None) -> str:
    """Кольцо → кусок path data. ccw задаёт направление обхода (для дырок)."""
    pts = _dedupe(ring)
    min_len = 4 if close else 2
    if len(pts) < min_len:
        return ""
    if ccw is not None:
        area = _signed_area(pts)
        if (area < 0) == ccw:      # хотим CCW, а намотано CW — разворот
            pts.reverse()
    coords = [_xy(p[0], p[1]) for p in pts]
    d = [f"M{_fmt(coords[0][0])} {_fmt(coords[0][1])}"]
    d += [f"L{_fmt(x)} {_fmt(y)}" for x, y in coords[1:]]
    if close:
        d.append("Z")
    return "".join(d)


def polygon_d(geometry: dict) -> str:
    """Polygon/MultiPolygon → path data с правильной намоткой дырок."""
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" \
        else [geometry["coordinates"]]
    parts = []
    for poly in polys:
        for idx, ring in enumerate(poly):
            # внешнее кольцо — CCW, дырки (озёра-гиганты) — CW
            d = ring_d(ring, close=True, ccw=(idx == 0))
            if d:
                parts.append(d)
    return "".join(parts)


def linestring_d(geometry: dict) -> str:
    lines = geometry["coordinates"] if geometry["type"] == "MultiLineString" \
        else [geometry["coordinates"]]
    parts = []
    for line in lines:
        d = ring_d(line, close=False)
        if d:
            parts.append(d)
    return "".join(parts)


# ── сборка слоёв ─────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def chunk_layer(d_strings: list[str]) -> list[str]:
    """Склеивает куски пути в <path>-элементы <= MAX_NUMBERS_PER_PATH чисел."""
    chunks, cur, cur_n = [], "", 0
    for d in d_strings:
        if not d:
            continue
        n = len(_NUM_RE.findall(d))
        if cur and cur_n + n > MAX_NUMBERS_PER_PATH:
            chunks.append(cur)
            cur, cur_n = "", 0
        cur += d
        cur_n += n
    if cur:
        chunks.append(cur)
    return chunks


def main() -> int:
    land = load_geojson("ne_110m_land.geojson")
    countries = load_geojson("ne_110m_admin_0_countries.geojson")
    rivers = load_geojson("ne_110m_rivers_lake_centerlines.geojson")

    # 1) суша (береговые линии материков и островов)
    land_parts = []
    for f in land["features"]:
        d = polygon_d(f["geometry"])
        if d:
            land_parts.append(d)

    # 2) границы только крупных стран
    border_parts = []
    for f in countries["features"]:
        if f["properties"].get("ISO_A3") in BIG_COUNTRIES_ISO3:
            d = polygon_d(f["geometry"])
            if d:
                border_parts.append(d)

    # 3) мегареки (в наборе 110m только они и есть)
    river_parts = []
    for f in rivers["features"]:
        if f["properties"].get("featurecla") != "River":
            continue
        d = linestring_d(f["geometry"])
        if d:
            river_parts.append(d)

    def count_pts(parts):
        return sum(len(_NUM_RE.findall(p)) for p in parts)

    print(f"слой land:   {len(land_parts)} полигонов, {count_pts(land_parts)} чисел")
    print(f"слой border: {len(border_parts)} стран, {count_pts(border_parts)} чисел")
    print(f"слой river:  {len(river_parts)} рек, {count_pts(river_parts)} чисел")

    layers = {
        "land": chunk_layer(land_parts),
        "border": chunk_layer(border_parts),
        "river": chunk_layer(river_parts),
    }

    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="180"'
        ' viewBox="0 0 360 180" version="1.1">',
        '<!-- UmbraNet world map: equirectangular, центр 0°.',
        '     Источник: Natural Earth 110m (public domain).',
        '     Сгенерировано tools/generate_world_map.py.',
        '     ИНВАРИАНТЫ: только команды M/L/Z; не более 24000 чисел на',
        '     <path> (лимит Qt SVG ~32k точек, QTBUG-120653); слои задаются',
        '     class="land|border|river" и читаются Map/core/map_dialog.py. -->',
    ]
    # Стили в самом SVG — чтобы фолбэк-рендер через QSvgRenderer (если
    # однажды появятся кривые) выглядел близко к основному виду.
    for chunk in layers["land"]:
        svg.append(f'<path class="land" fill="#0a2416" fill-opacity="0.92"'
                   f' stroke="#2ee06a" stroke-opacity="0.8" stroke-width="0.6" d="{chunk}"/>')
    for chunk in layers["border"]:
        svg.append(f'<path class="border" fill="none" stroke="#2ee06a"'
                   f' stroke-opacity="0.5" stroke-width="0.42" d="{chunk}"/>')
    for chunk in layers["river"]:
        svg.append(f'<path class="river" fill="none" stroke="#5ba8c9"'
                   f' stroke-opacity="0.65" stroke-width="0.38" d="{chunk}"/>')
    svg.append("</svg>\n")

    out = "\n".join(svg)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"записан {OUT_PATH} ({len(out) / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
