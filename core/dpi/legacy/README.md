# Legacy DPI Engine

`dpi_engine.py` — экспериментальный движок на `pydivert` / WinDivert.

**Статус:** DEPRECATED с 2026-09-14. Production-путь — `bin/winws.exe` через `core/dpi/winws_engine.py`.

Файл оставлен только для истории и локальных экспериментов. Новые фичи пишите через `StrategyManager` + `WinWSEngine`.

Удаление запланировано после 2027-01-01, если не всплывут зависимости.

Импорт shim: `core/dpi/dpi_engine.py` реэкспортирует этот файл с DeprecationWarning.
