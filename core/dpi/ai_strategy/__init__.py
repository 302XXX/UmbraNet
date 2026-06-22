"""
UmbraNet AI Strategy / Autotuner foundation.

Пакет хранит фундамент будущей генерации Uz:
  • generation_targets — на чём строится проверка DPI-метода;
  • mask_services — под какие профили можно маскироваться;
  • seeds — стартовые точки генерации;
  • mutations — контролируемое создание временных вариантов;
  • probes/scoring — проверка и оценка;
  • session/autotuner — controlled generation plan.

На текущем этапе пакет ничего не запускает и не меняет в системе: он только
строит технический план для будущей генерации.
"""

from __future__ import annotations

from .autotuner import plan_autotune
from .masks import load_mask_services
from .mutations import generate_variants
from .probes import probe_discord_basic, probe_youtube_basic, run_basic_probes
from .scoring import choose_best, score_probe_result, score_variant
from .seeds import list_seeds
from .session import ControlledGenerationSession, plan_generation_session
from .targets import analyze_generation_targets, load_generation_targets

__all__ = [
    "ControlledGenerationSession",
    "analyze_generation_targets",
    "choose_best",
    "generate_variants",
    "list_seeds",
    "load_generation_targets",
    "load_mask_services",
    "plan_autotune",
    "plan_generation_session",
    "probe_discord_basic",
    "probe_youtube_basic",
    "run_basic_probes",
    "score_probe_result",
    "score_variant",
]
