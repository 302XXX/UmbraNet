"""
UmbraNet — Автозапуск с Windows (Task Scheduler)
=================================================

Управляет автозапуском программы при входе в Windows через Планировщик задач.

Почему именно Task Scheduler (schtasks):
  • Позволяет запускать программу сразу с правами администратора (/RL HIGHEST),
    что избавляет пользователя от назойливого окна UAC при каждой загрузке ПК.
  • Работает абсолютно незаметно.

API:
  is_supported()      -> bool   — поддерживается ли автозапуск на этой ОС
  is_enabled()        -> bool   — включён ли сейчас
  enable()            -> (ok, msg)
  disable()           -> (ok, msg)
  set_enabled(flag)   -> (ok, msg)
"""

import logging
import os
import sys
import subprocess
import tempfile
import xml.sax.saxutils

log = logging.getLogger("UmbraNet.Autostart")

IS_WINDOWS = sys.platform == "win32"
TASK_NAME = "UmbraNet_Autostart"


def is_supported() -> bool:
    return IS_WINDOWS


def _get_paths() -> tuple[str, str]:
    """Возвращает (путь_к_python, путь_к_start_pyw)."""
    python_exe = sys.executable
    if "python.exe" in python_exe.lower() and "pythonw.exe" not in python_exe.lower():
        pw = python_exe.lower().replace("python.exe", "pythonw.exe")
        if os.path.exists(pw):
            python_exe = pw

    core_dir = os.path.dirname(os.path.abspath(__file__))
    app_dir = os.path.dirname(core_dir)
    start_pyw = os.path.join(app_dir, "start.pyw")
    
    return python_exe, start_pyw


def is_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        res = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return res.returncode == 0
    except Exception:
        return False


def enable() -> tuple:
    if not IS_WINDOWS:
        return False, "Автозапуск доступен только в Windows"
    
    # 1. Удаляем старый автозапуск из реестра (если остался от прошлых версий)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, "UmbraNet")
    except Exception:
        pass

    try:
        python_exe, start_pyw = _get_paths()
        python_exe_xml = xml.sax.saxutils.escape(python_exe)
        start_pyw_xml = f'"{xml.sax.saxutils.escape(start_pyw)}"'

        # Генерируем XML задачи для тонкой настройки (например, работа от батареи)
        xml_data = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_exe_xml}</Command>
      <Arguments>{start_pyw_xml}</Arguments>
    </Exec>
  </Actions>
</Task>
"""
        fd, tmp_path = tempfile.mkstemp(suffix=".xml")
        with open(fd, "w", encoding="utf-16") as f:
            f.write(xml_data)

        try:
            res = subprocess.run(
                ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp_path, "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            
            if res.returncode == 0:
                log.info("Тихий автозапуск включён через Task Scheduler")
                return True, "Тихий автозапуск включён (без окна UAC)"
            else:
                err = res.stderr.decode("utf-8", errors="replace").strip() or res.stdout.decode("utf-8", errors="replace").strip()
                if not err:
                    err = res.stderr.decode("cp866", errors="replace").strip()
                log.error(f"Ошибка schtasks: {err}")
                return False, f"Ошибка: {err}"
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    except Exception as exc:
        log.warning("Не удалось включить автозапуск: %s", exc)
        return False, f"Не удалось включить автозапуск: {exc}"


def disable() -> tuple:
    if not IS_WINDOWS:
        return False, "Автозапуск доступен только в Windows"
    
    # Также подчищаем реестр на всякий случай
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, "UmbraNet")
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        
        err = res.stderr.decode("utf-8", errors="replace").lower()
        if res.returncode == 0 or "соответствует" in err or "not find" in err or "не найдено" in err:
            log.info("Автозапуск выключен")
            return True, "Автозапуск выключен"
        else:
            log.error(f"Ошибка schtasks: {err}")
            return False, f"Ошибка выключения: {err.strip()}"
    except Exception as exc:
        log.warning("Не удалось выключить автозапуск: %s", exc)
        return False, f"Не удалось выключить автозапуск: {exc}"


def set_enabled(flag: bool) -> tuple:
    return enable() if flag else disable()
