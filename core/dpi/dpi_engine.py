"""
DEPRECATED — UmbraNet DPIEngine (legacy / pydivert).

Этот модуль сохранён только для совместимости и отладки.
Production-путь с 2026 года — bin/winws.exe через WinWSEngine.

НЕ используйте в новом коде. Для запуска DPI используйте:
  from winws_engine import get_winws_engine
  from strategy_manager import get_strategy_manager

Файл будет удалён после 2027-01-01. См. PLAN_STAGE1.md.
"""
from __future__ import annotations

import warnings

warnings.warn(
    "core.dpi.dpi_engine is deprecated: use winws_engine + strategy_manager. "
    "Legacy file moved to core/dpi/legacy/dpi_engine.py",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export legacy implementation so старые импорты не ломаются
try:
    from core.dpi.legacy.dpi_engine import *  # noqa: F401,F403
    from core.dpi.legacy.dpi_engine import DPIEngine  # noqa: F401
except Exception:  # pragma: no cover — если лежит как legacy import
    try:
        from legacy.dpi_engine import *  # noqa: F401,F403
        from legacy.dpi_engine import DPIEngine  # noqa: F401
    except Exception:
        # Fallback: прямая загрузка файла по пути (работает при flat sys.path)
        try:
            import importlib.util as _ilu
            import pathlib as _pl
            _legacy_path = _pl.Path(__file__).with_name("legacy") / "dpi_engine.py"
            _spec = _ilu.spec_from_file_location("_umbranet_legacy_dpi", str(_legacy_path))
            _mod = _ilu.module_from_spec(_spec)  # type: ignore
            assert _spec and _spec.loader
            _spec.loader.exec_module(_mod)  # type: ignore
            DPIEngine = getattr(_mod, "DPIEngine", None)
            # экспорт остальных имён на случай star-import
            for _k in dir(_mod):
                if not _k.startswith("_"):
                    globals()[_k] = getattr(_mod, _k)
        except Exception:
            DPIEngine = None  # type: ignore
