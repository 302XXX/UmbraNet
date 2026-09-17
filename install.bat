@echo off
chcp 65001 >nul
REM =====================================================================
REM  UmbraNet - полный установщик зависимостей
REM
REM  Что делает:
REM    1. Проверяет Python 3.10+
REM    2. Создаёт .venv рядом с программой
REM    3. Обновляет pip/setuptools/wheel
REM    4. Ставит ВСЕ компоненты DNS: PySide6, dnslib, requests, psutil,
REM       aioquic (DoQ), pynacl (DNSCrypt)
REM    5. Пытается поставить pydivert для DPI-режима
REM
REM  Важно:
REM    - chcp 65001 нужен, чтобы русские буквы не превращались в кракозябры.
REM    - PySide6 содержит очень длинные внутренние пути. Если программа лежит
REM      глубоко в Downloads/workspace/..., pip может упасть с Errno 2 / Long Path.
REM      Поэтому установщик при длинном пути сам перезапускается через короткий
REM      временный subst-диск.
REM =====================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "APP_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_NO_CACHE_DIR=1"

REM ── Авто-обход Windows MAX_PATH для PySide6 ───────────────────────────
REM Если путь к папке длинный, делаем временный диск U:/V:/... на эту папку
REM и запускаем install.bat уже как U:\install.bat. Это короче, чем просить
REM пользователя включать LongPathsEnabled в реестре.
if not defined UMBRANET_SUBST_DONE (
    if not "!APP_DIR:~80,1!"=="" (
        echo.
        echo  [INFO] Путь к UmbraNet слишком длинный для установки PySide6:
        echo         !APP_DIR!
        echo  [INFO] Пробую временно запустить установку через короткий диск...
        echo.
        for %%D in (U V W X Y Z) do (
            subst %%D: "%~dp0" >nul 2>nul
            if not errorlevel 1 (
                set "UMBRANET_SUBST_DONE=1"
                pushd %%D:\
                call install.bat
                set "RC=!errorlevel!"
                popd
                subst %%D: /D >nul 2>nul
                exit /b !RC!
            )
        )
        echo  [ПРЕДУПРЕЖДЕНИЕ] Не удалось создать временный короткий диск.
        echo  Если установка PySide6 упадёт с Long Path, перенесите папку в C:\UmbraNet.
        echo.
    )
)

echo.
echo  =============================================
echo    UmbraNet ^| Полная установка зависимостей
echo  =============================================
echo.

REM ── Поиск Python ─────────────────────────────────────────────────────
set "SYS_PY="
for /f "delims=" %%i in ('where py 2^>nul') do if not defined SYS_PY set "SYS_PY=py -3"
if not defined SYS_PY (
    for /f "delims=" %%i in ('where python 2^>nul') do if not defined SYS_PY set "SYS_PY=%%i"
)
if not defined SYS_PY (
    echo  [ОШИБКА] Python не найден.
    echo.
    echo  Установите Python 3.10+ с https://www.python.org/downloads/
    echo  и включите галочку "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo  Используется Python:
%SYS_PY% --version
if errorlevel 1 (
    echo  [ОШИБКА] Не удалось запустить Python.
    pause
    exit /b 1
)

%SYS_PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo  [ОШИБКА] Нужен Python 3.10 или новее.
    pause
    exit /b 1
)

REM ── venv ─────────────────────────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  Создание виртуального окружения .venv ...
    %SYS_PY% -m venv .venv
    if errorlevel 1 (
        echo  [ОШИБКА] Не удалось создать .venv.
        pause
        exit /b 1
    )
) else (
    echo.
    echo  [OK] .venv уже существует.
)

set "PY=.venv\Scripts\python.exe"

echo.
echo  Обновление pip / setuptools / wheel ...
"%PY%" -m pip install --upgrade --no-cache-dir pip setuptools wheel
if errorlevel 1 (
    echo  [ОШИБКА] Не удалось обновить pip.
    pause
    exit /b 1
)

REM ── Основные и encrypted DNS зависимости ─────────────────────────────
echo.
echo  -----------------------------------------------
echo   Установка обязательных компонентов
echo   GUI + DNS + DoQ + DNSCrypt
echo  -----------------------------------------------
"%PY%" -m pip install --upgrade --no-cache-dir ^
    "PySide6>=6.7.0" ^
    "dnslib>=0.9.24" ^
    "requests>=2.28.0" ^
    "psutil>=5.9.0" ^
    "aioquic>=1.0.0" ^
    "pynacl>=1.5.0"
if errorlevel 1 (
    echo.
    echo  [ОШИБКА] Не удалось установить обязательные компоненты.
    echo.
    echo  Частая причина: Windows Long Path / слишком длинный путь к папке.
    echo  Текущий путь:
    echo    %CD%
    echo.
    echo  Что попробовать:
    echo    1^) Закройте все окна UmbraNet и удалите папку .venv, затем запустите install.bat снова.
    echo    2^) Перенесите папку UmbraNet ближе к корню диска, например C:\UmbraNet.
    echo    3^) Включите Windows Long Path Support:
    echo       reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f
    echo       После этого перезагрузите Windows.
    echo    4^) Проверьте интернет / прокси / антивирус.
    echo.
    pause
    exit /b 1
)

REM ── Проверка импортов ────────────────────────────────────────────────
echo.
echo  Проверка установленных модулей ...
"%PY%" -c "import PySide6, dnslib, requests, psutil, aioquic, nacl; print('OK: all required modules imported')"
if errorlevel 1 (
    echo  [ОШИБКА] Модули установились не полностью. Повторите install.bat.
    echo  Если ошибка повторяется, удалите папку .venv и запустите install.bat заново.
    pause
    exit /b 1
)

REM ── DPI dependency, пока не критично ─────────────────────────────────
echo.
echo  -----------------------------------------------
echo   DPI-компонент pydivert ^(для DPI режима^)
echo  -----------------------------------------------
echo  Если установка pydivert не получится, DNS / DoH / DoQ / DNSCrypt всё равно работают.
echo.
"%PY%" -m pip install --upgrade --no-cache-dir "pydivert>=2.1.0"
if errorlevel 1 (
    echo  [ПРЕДУПРЕЖДЕНИЕ] pydivert не установлен. DPI-часть через pydivert будет недоступна.
) else (
    echo  [OK] pydivert установлен.
)

REM ── Итог ─────────────────────────────────────────────────────────────
echo.
echo  =============================================
echo   Готово!
echo   Установлены: UDP / DoH / DoT / DoQ / DNSCrypt + WinWS
echo   Запуск: start.bat
echo  =============================================
echo.
pause
endlocal
