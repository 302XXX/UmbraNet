"""
Autotune planner for UmbraNet AI strategies.

Текущий этап: dry-run планировщик. Он ничего не запускает, не меняет активную
стратегию и не пишет новые Uz-файлы. Задача — дать будущему UI/engine_adapter
технический план: generation targets, текущий main-menu hostlist, masks,
seed-шаблоны, мутации и controlled session policy.
"""

from __future__ import annotations

from typing import Any

from .masks import default_mask_ids, list_default_masks, load_mask_services
from .mutations import generate_variants
from .seeds import list_seeds
from .session import plan_generation_session
from .targets import analyze_generation_targets


def plan_autotune(config: dict[str, Any] | None = None, mode: str = "quick") -> dict[str, Any]:
    """Строит технический план будущей генерации Uz.

    Генерация строится на generation targets (по умолчанию YouTube + Discord),
    а не напрямую на сервисах главного меню. Главное меню фиксируется отдельно:
    итоговая стратегия будет применяться к его hostlist, но тестовая база
    генерации остаётся контролируемой.
    """
    target_analysis = analyze_generation_targets(config)
    masks_data = load_mask_services()
    masks = list_default_masks(masks_data)
    seeds = list_seeds(mode=mode)
    variants = generate_variants(mode=mode, max_variants=12 if mode != "deep" else 30)
    session = plan_generation_session(config, mode=mode)

    return {
        "schema_version": 1,
        "mode": mode,
        "stage": "dry_run_plan",
        "generation": target_analysis,
        "mask_ids": default_mask_ids(masks_data),
        "masks": masks,
        "seeds": [
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "risk": s.get("risk"),
                "priority": s.get("priority"),
                "args_count": len(s.get("args", []) or []),
            }
            for s in seeds
        ],
        "variants": {
            "count": len(variants),
            "preview": [
                {
                    "id": v.get("id"),
                    "seed_id": v.get("seed_id"),
                    "mutation": v.get("mutation"),
                    "mask_id": v.get("mask_id"),
                    "risk": v.get("risk"),
                    "args_count": v.get("args_count"),
                }
                for v in variants[:8]
            ],
        },
        # compatibility for older plan readers while UI is not connected yet
        "candidates": [
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "risk": s.get("risk"),
                "priority": s.get("priority"),
                "args_count": len(s.get("args", []) or []),
            }
            for s in seeds
        ],
        "session": session,
        "probes": {
            "basic_available": ["youtube", "discord"],
            "browser_video_available": False,
            "strategy_switching_available": False,
        },
        "limits": {
            "quick_variants": 12,
            "deep_variants": 30,
            "quick_seeds": 3,
            "deep_seeds": len(list_seeds(mode="deep")),
        },
    }
