"""Pytest fixtures — добавляем core в sys.path как делает engine_adapter."""
import os
import sys

# Корень проекта
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CORE = os.path.join(ROOT, "core")
DNS = os.path.join(CORE, "dns")
DPI = os.path.join(CORE, "dpi")

for p in (CORE, DNS, DPI, os.path.join(DPI, "ai_strategy"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
