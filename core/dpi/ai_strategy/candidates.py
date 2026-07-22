"""
Backward-compatible alias for old imports.

AI generation now treats these predefined variants as seed templates, not as
final user-facing candidates.
"""

from __future__ import annotations

from .seeds import *  # noqa: F401,F403
