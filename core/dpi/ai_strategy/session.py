"""
Controlled AI generation session model.

Это пока не исполнитель, а безопасный скелет будущей полноценной генерации.
Полноценная сессия должна:
  1. запросить подтверждение пользователя в UI;
  2. остановить UmbraNet так же, как кнопка Stop;
  3. тестировать временные варианты из mutations.py в изоляции;
  4. выбрать лучший через scoring.py;
  5. сохранить только финальную Uz, если score достаточный;
  6. восстановить предыдущее состояние в finally.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .mutations import generate_variants


@dataclass
class SessionPolicy:
    requires_user_confirm: bool = True
    requires_stop: bool = True
    restore_previous_state: bool = True
    test_mode: str = "combo"
    isolation: str = "stop_start_each_variant"
    min_score_to_save: int = 70
    quick_time_limit_sec: int = 180
    deep_time_limit_sec: int = 600
    quick_max_variants: int = 18
    deep_max_variants: int = 30
    per_variant_timeout_sec: int = 35


def session_policy(mode: str = "quick") -> dict[str, Any]:
    policy = asdict(SessionPolicy())
    deep = mode == "deep"
    data = dict(policy)
    data["time_limit_sec"] = data["deep_time_limit_sec"] if deep else data["quick_time_limit_sec"]
    data["max_variants"] = data["deep_max_variants"] if deep else data["quick_max_variants"]
    data.pop("quick_time_limit_sec", None)
    data.pop("deep_time_limit_sec", None)
    data.pop("quick_max_variants", None)
    data.pop("deep_max_variants", None)
    return data


def plan_generation_session(config: dict[str, Any] | None = None,
                            mode: str = "quick") -> dict[str, Any]:
    """План контролируемой генерации без фактического запуска."""
    policy = session_policy(mode)
    max_variants = int(policy.get("max_variants", 12) or 12)
    variants = generate_variants(mode=mode, max_variants=max_variants)
    return {
        "stage": "controlled_generation_plan",
        "mode": mode,
        "policy": policy,
        "variants_count": len(variants),
        "variants_preview": [
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
        "will_save_only_if_score_at_least": policy.get("min_score_to_save", 70),
    }


class ControlledGenerationSession:
    """Заготовка будущего исполнителя генерации.

    Сейчас intentionally dry-run: чтобы не останавливать процессы и не менять
    сеть, пока UI/engine lifecycle не будут подключены явно.
    """

    def __init__(self, config: dict[str, Any] | None = None, mode: str = "quick"):
        self.config = config or {}
        self.mode = mode
        self.policy = session_policy(mode)
        self.variants = generate_variants(mode=mode, max_variants=int(self.policy.get("max_variants", 12)))

    def dry_run(self) -> dict[str, Any]:
        return plan_generation_session(self.config, self.mode)

    def run(self) -> dict[str, Any]:
        raise NotImplementedError(
            "ControlledGenerationSession.run будет подключён после реализации "
            "безопасного stop/start lifecycle и временного запуска вариантов."
        )
