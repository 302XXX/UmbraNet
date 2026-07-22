"""
Controlled mutations for AI-generated Uz variants.

Это не случайная генерация. Мутации ограничены безопасными изменениями seed:
  • изменить repeats в разумном диапазоне;
  • заменить fake TLS/QUIC профиль;
  • убрать UDP/QUIC-блок для TCP-focused проверки;
  • усилить или смягчить вариант.

На этом этапе модуль только строит временные варианты в памяти. Он ничего не
запускает, не пишет в strategies и не меняет активную стратегию.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .masks import get_mask_profile
from .seeds import list_seeds

MIN_REPEATS = 1
MAX_REPEATS = 15


def _clamp(value: int, low: int = MIN_REPEATS, high: int = MAX_REPEATS) -> int:
    return max(low, min(high, int(value)))


def _replace_repeats(args: list[str], delta: int = 0, fixed: int | None = None) -> list[str]:
    out: list[str] = []
    for arg in args:
        if arg.startswith("--dpi-desync-repeats="):
            try:
                old = int(arg.split("=", 1)[1])
            except Exception:
                old = 8
            new = _clamp(fixed if fixed is not None else old + int(delta))
            out.append(f"--dpi-desync-repeats={new}")
        else:
            out.append(arg)
    return out


def _replace_fake_bins(args: list[str], mask_id: str) -> list[str]:
    profile = get_mask_profile(mask_id)
    bins = profile.get("bin") if isinstance(profile.get("bin"), dict) else {}
    tls = str(bins.get("tls", "") or "")
    quic = str(bins.get("quic", "") or "")
    if not tls and not quic:
        return list(args)

    out: list[str] = []
    for arg in args:
        if tls and (arg.startswith("--dpi-desync-fake-tls=") or arg.startswith("--dpi-desync-split-seqovl-pattern=")):
            key = arg.split("=", 1)[0]
            out.append(f"{key}={tls}")
        elif quic and arg.startswith("--dpi-desync-fake-quic="):
            out.append(f"--dpi-desync-fake-quic={quic}")
        else:
            out.append(arg)
    return out


def _remove_udp_blocks(args: list[str]) -> list[str]:
    """Убирает UDP/QUIC блоки из args, оставляя TCP-секции.

    Работает с winws-разделителями --new. Если секция содержит --filter-udp
    или --wf-udp без TCP-фильтра, секция удаляется.
    """
    sections: list[list[str]] = [[]]
    for arg in args:
        if arg == "--new":
            sections.append([])
        else:
            sections[-1].append(arg)

    kept: list[list[str]] = []
    global_args: list[str] = []
    for idx, section in enumerate(sections):
        has_udp = any(a.startswith("--filter-udp") or a.startswith("--wf-udp") for a in section)
        has_tcp_filter = any(a.startswith("--filter-tcp") for a in section)
        if idx == 0:
            # В первой секции могут быть глобальные --wf-tcp/--wf-udp. Сохраняем
            # TCP-глобалы, UDP-глобалы выкидываем.
            filtered = [a for a in section if not a.startswith("--wf-udp")]
            if has_udp and not has_tcp_filter:
                global_args = [a for a in filtered if a.startswith("--wf-tcp")]
            else:
                kept.append(filtered)
        elif has_udp and not has_tcp_filter:
            continue
        else:
            kept.append(section)

    if global_args and kept:
        kept[0] = global_args + [a for a in kept[0] if not a.startswith("--wf-tcp")]
    elif global_args and not kept:
        kept.append(global_args)

    out: list[str] = []
    for idx, section in enumerate(kept):
        if idx > 0:
            out.append("--new")
        out.extend(section)
    return out


def mutate_seed(seed: dict[str, Any], mutation_id: str,
                mask_id: str | None = None) -> dict[str, Any]:
    """Создаёт один временный вариант из seed."""
    base_args = list(seed.get("args", []) or [])
    args = list(base_args)
    mid = str(mask_id or "").strip().lower()

    if mutation_id == "base":
        pass
    elif mutation_id == "soft_repeats":
        args = _replace_repeats(args, delta=-2)
    elif mutation_id == "strong_repeats":
        args = _replace_repeats(args, delta=2)
    elif mutation_id == "tcp_only":
        args = _remove_udp_blocks(args)
    elif mutation_id == "low_repeats":
        args = _replace_repeats(args, fixed=4)
    elif mutation_id == "high_repeats":
        args = _replace_repeats(args, fixed=11)

    if mid:
        args = _replace_fake_bins(args, mid)

    seed_id = str(seed.get("id", "seed"))
    suffix = mutation_id if not mid else f"{mutation_id}_{mid}"
    return {
        "id": f"{seed_id}__{suffix}",
        "seed_id": seed_id,
        "mutation": mutation_id,
        "mask_id": mid or "seed_default",
        "title": seed.get("title", seed_id),
        "risk": seed.get("risk", "medium"),
        "args": args,
        "args_count": len(args),
        "save_priority": int(seed.get("save_priority", seed.get("priority", 50)) or 50),
        "meta": {
            "seed_priority": seed.get("priority"),
            "save_priority": seed.get("save_priority", seed.get("priority")),
            "seed_description": seed.get("description", ""),
        },
    }


def generate_variants(mode: str = "quick", masks: list[str] | None = None,
                      max_variants: int | None = None) -> list[dict[str, Any]]:
    """Генерирует временные варианты из seed-шаблонов.

    quick: компактный набор для будущей быстрой генерации.
    deep: больше seed и мутаций.
    """
    mask_ids = masks or ["google", "max_ru"]
    seeds = list_seeds(mode="deep" if mode == "deep" else "quick")
    mutation_ids = ["base", "soft_repeats", "strong_repeats", "tcp_only"]
    if mode == "deep":
        mutation_ids += ["low_repeats", "high_repeats"]
        mask_ids = list(dict.fromkeys(mask_ids + ["stun"]))

    variants: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    def add_variant(seed: dict[str, Any], mutation_id: str, mask_id: str = "") -> bool:
        variant = mutate_seed(seed, mutation_id, mask_id=mask_id or None)
        sig = tuple(variant.get("args", []) or [])
        if sig in seen:
            return False
        seen.add(sig)
        variants.append(variant)
        return bool(max_variants and len(variants) >= max_variants)

    # Важно: порядок теперь фазовый, а не "все мутации первого seed".
    # Так генерация с лимитом 12-18 вариантов успевает проверить balanced,
    # TCP и UDP/QUIC подходы, а не застревает на minimal seed.
    for mutation_id in mutation_ids:
        profiles = [""] if mutation_id == "base" else mask_ids
        for seed in seeds:
            for mask_id in profiles:
                if add_variant(seed, mutation_id, mask_id):
                    return variants
    return variants
