@echo off
chcp 65001 >nul
REM =====================================================================
REM  UmbraNet - Запуск от имени администратора
REM
REM  Права администратора нужны для:
REM    - DNS-сервер на порту 53 (127.0.0.1:53 / ::1:53)
REM    - DPI-движок (управление пакетами через WinDivert)
REM    - Смена системного DNS на 127.0.0.1
REM
REM  Скрипт автоматически запрашивает повышение прав (UAC).
REM
REM  ВАЖНО: запускаем start.pyw, а не "python -m umbranet.main".
REM  При UAC-повышении Windows меняет рабочую директорию на System32,
REM  и "python -m umbranet.main" перестаёт находить пакет umbranet.
REM  start.pyw добавляет путь к программе в sys.path по абсолютному
REM  пути через __file__ — работает независимо от рабочей директории.
REM =====================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

REM ── Шаг 1: проверка прав администратора ─────────────────────────────
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  Запрос прав администратора...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

REM ── Сюда попадаем только с правами администратора ────────────────────
cd /d "%~dp0"
set "APP_DIR=%~dp0"

REM ── Обход длинного пути для запуска PySide6/Qt ───────────────────────
REM install.bat ставит зависимости через короткий subst-диск. При запуске
REM тоже используем короткий путь, иначе Qt/PySide6 иногда молча падает,
REM если UmbraNet лежит глубоко в Downloads/workspace/...
if not defined UMBRANET_SUBST_DONE (
    if not "!APP_DIR:~80,1!"=="" (
        for %%D in (U V W X Y Z) do (
            subst %%D: "%~dp0" >nul 2>nul
            if not errorlevel 1 (
                set "UMBRANET_SUBST_DONE=1"
                pushd %%D:\
                call start.bat
                set "RC=!errorlevel!"
                popd
                REM Не удаляем subst-диск сразу: pythonw/Qt продолжает грузить
                REM DLL/QML уже после выхода batch-файла. Маппинг безвреден и
                REM исчезнет после перезагрузки или ручного `subst %%D: /D`.
                exit /b !RC!
            )
        )
    )
)

REM ── Шаг 2: выбор Python-интерпретатора ──────────────────────────────
REM Приоритет: .venv из install.bat, иначе системный pythonw/python.
set "PYEXE="

if exist "%~dp0.venv\Scripts\pythonw.exe" (
    set "PYEXE=%~dp0.venv\Scripts\pythonw.exe"
    goto :found_python
)
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYEXE=%~dp0.venv\Scripts\python.exe"
    goto :found_python
)

REM Системный Python — ищем pythonw (без консоли), потом python
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%i"
)
if not defined PYEXE (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%i"
    )
)

:found_python
if not defined PYEXE (
    echo  [ОШИБКА] Python не найден.
    echo.
    echo  Запустите install.bat для установки зависимостей.
    pause
    exit /b 1
)

REM ── Шаг 3: запуск через start.pyw ───────────────────────────────────
REM "start """ запускает программу в отдельном процессе и сразу
REM закрывает это консольное окно. С pythonw.exe консоли вообще нет.
start "" "%PYEXE%" "%~dp0start.pyw"
exit /b 0
