"""
Scoring for AI Uz generation.

Оценка нужна, чтобы генератор мог сравнивать временные варианты. На текущем
уровне probes YouTube/Discord ещё не отличают «страница доступна» от
«реальный звонок/видео работает идеально», поэтому Discord получает больший
вес, а помимо сетевого score учитывается
save_priority seed-шаблона. Это не даёт минимальному diagnostic seed побеждать
полноценную Uz1-like стратегию только из-за low-risk.
"""

from __future__ import annotations

from typing import Any

RISK_PENALTY = {
    "low": 0,
    "medium": 4,
    "high": 10,
}


def score_probe_result(probe_result: dict[str, Any]) -> dict[str, Any]:
    services = probe_result.get("services", []) if isinstance(probe_result, dict) else []
    service_scores: dict[str, int] = {}
    ok_services = 0
    total = 0
    for service in services or []:
        if not isinstance(service, dict):
            continue
        name = str(service.get("service", "unknown"))
        score = int(service.get("score", 0) or 0)
        service_scores[name] = score
        total += score
        if service.get("ok"):
            ok_services += 1
    # Discord для UmbraNet важен не только как сайт, но и как звонки. Поэтому
    # при сравнении стратегий даём ему больший вес: стратегия, где YouTube ок,
    # но Discord voice_readiness слабый, не должна побеждать.
    if "discord" in service_scores and "youtube" in service_scores:
        avg = round(service_scores.get("discord", 0) * 0.60 + service_scores.get("youtube", 0) * 0.40)
    else:
        count = max(len(service_scores), 1)
        avg = round(total / count)
    return {
        "score": avg,
        "ok": ok_services == len(service_scores) and bool(service_scores),
        "ok_services": ok_services,
        "services_count": len(service_scores),
        "service_scores": service_scores,
    }


def _save_priority_bonus(save_priority: int) -> int:
    """Небольшой бонус полноценным seed при равных basic-probes.

    Диапазон намеренно маленький: probes остаются главным фактором, но пока
    нет video playback probe, рекомендуемая Uz1-like база должна выигрывать
    у минимального диагностического split.
    """
    try:
        p = max(0, min(100, int(save_priority)))
    except Exception:
        p = 50
    return round((p - 50) / 10)  # примерно -5..+5


def score_variant(variant: dict[str, Any], probe_result: dict[str, Any]) -> dict[str, Any]:
    base = score_probe_result(probe_result)
    risk = str(variant.get("risk", "medium")).lower()
    penalty = RISK_PENALTY.get(risk, 4)
    save_priority = int(variant.get("save_priority", 50) or 50)
    priority_bonus = _save_priority_bonus(save_priority)
    final = max(0, min(100, int(base.get("score", 0)) - penalty + priority_bonus))
    return {
        "variant_id": variant.get("id"),
        "seed_id": variant.get("seed_id"),
        "mutation": variant.get("mutation"),
        "mask_id": variant.get("mask_id"),
        "risk": risk,
        "raw_score": int(base.get("score", 0)),
        "risk_penalty": penalty,
        "save_priority": save_priority,
        "priority_bonus": priority_bonus,
        "score": final,
        "ok": bool(base.get("ok")) and final >= 70,
        "service_scores": base.get("service_scores", {}),
    }


def choose_best(scored_variants: list[dict[str, Any]], min_score: int = 70) -> dict[str, Any]:
    if not scored_variants:
        return {"ok": False, "reason": "no_variants", "best": None}
    ordered = sorted(
        scored_variants,
        key=lambda x: (
            int(x.get("score", 0)),
            int(x.get("save_priority", 0)),
            -int(x.get("risk_penalty", 0)),
        ),
        reverse=True,
    )

    # ВАЖНО: раньше выбор смотрел только на score. Это могло сохранить Uz,
    # где общий балл >= 70, но обязательный Discord gateway/voice_readiness
    # провален. Теперь стратегия может быть сохранена только если score_variant
    # выставил ok=True, то есть обязательные service probes прошли.
    best = ordered[0]
    acceptable = [
        x for x in ordered
        if bool(x.get("ok")) and int(x.get("score", 0)) >= int(min_score)
    ]
    if acceptable:
        best_ok = acceptable[0]
        return {
            "ok": True,
            "reason": "ok",
            "best": best_ok,
            "min_score": int(min_score),
        }

    reason = "below_threshold"
    if int(best.get("score", 0)) >= int(min_score) and not bool(best.get("ok")):
        reason = "required_probes_failed"
    return {
        "ok": False,
        "reason": reason,
        "best": best,
        "min_score": int(min_score),
    }
