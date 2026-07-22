"""
Mask service catalog for future Uz generation.

Mask services — это НЕ цели разблокировки. Это каталог профилей/доменов,
под которые будущие DPI-кандидаты могут маскировать fake-пакеты или выбирать
готовые bin-шаблоны.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
MASK_SERVICES_FILE = DATA_DIR / "mask_services.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_mask_services() -> dict[str, Any]:
    """Загружает data/mask_services.json."""
    data = _load_json(MASK_SERVICES_FILE)
    data.setdefault("default", [])
    data.setdefault("masks", {})
    return data


def default_mask_ids(data: dict[str, Any] | None = None) -> list[str]:
    data = data or load_mask_services()
    masks = data.get("masks") if isinstance(data.get("masks"), dict) else {}
    ids: list[str] = []
    for mid in data.get("default", []) or []:
        mid = str(mid).strip().lower()
        if mid and mid in masks and mid not in ids:
            ids.append(mid)
    return ids


def get_mask_profile(mask_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load_mask_services()
    masks = data.get("masks") if isinstance(data.get("masks"), dict) else {}
    mid = str(mask_id or "").strip().lower()
    item = masks.get(mid, {}) if isinstance(masks.get(mid), dict) else {}
    return {"id": mid, **item} if item else {}


def list_default_masks(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = data or load_mask_services()
    return [get_mask_profile(mid, data) for mid in default_mask_ids(data)]


def select_bin(mask_id: str, kind: str, fallback: str = "") -> str:
    """Возвращает путь-шаблон bin из mask profile, если он задан."""
    profile = get_mask_profile(mask_id)
    bins = profile.get("bin") if isinstance(profile.get("bin"), dict) else {}
    value = bins.get(kind) if isinstance(bins, dict) else ""
    return str(value or fallback or "")
