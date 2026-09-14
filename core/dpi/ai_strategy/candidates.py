"""
Backward-compatible alias for old imports.

AI generation now treats these predefined variants as seed templates, not as
final user-facing candidates.
"""

from __future__ import annotations

from .seeds import (
    CANDIDATES,
    GOOGLE_QUIC,
    GOOGLE_TLS,
    MAX_TLS,
    SEEDS,
    STUN_BIN,
    get_candidate,
    get_seed,
    list_candidates,
    list_seeds,
)

__all__ = [
    "CANDIDATES",
    "GOOGLE_QUIC",
    "GOOGLE_TLS",
    "MAX_TLS",
    "SEEDS",
    "STUN_BIN",
    "get_candidate",
    "get_seed",
    "list_candidates",
    "list_seeds",
]
