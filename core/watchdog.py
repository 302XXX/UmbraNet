"""
UmbraNet — Watchdog Process
===========================

Микро-скрипт, который защищает интернет пользователя при краше основной программы.
Он запускается как отдельный процесс, получает PID основного приложения и спит.
Если основной PID исчез из системы (программа вылетела) — скрипт сбрасывает
системный DNS Windows на Авто (DHCP) и закрывается.

Если основная программа завершается штатно, она сама убивает этот процесс.
"""

import sys
import time
import ctypes
import subprocess
import os

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def reset_dns_to_auto():
    """Аварийный сброс DNS через PowerShell."""
    ps_cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "foreach ($a in $adapters) { "
        "  Set-DnsClientServerAddress -InterfaceAlias $a.Name -ResetServerAddresses -ErrorAction SilentlyContinue; "
        "}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )

def main():
    if len(sys.argv) < 2:
        sys.exit(1)
        
    try:
        parent_pid = int(sys.argv[1])
    except ValueError:
        sys.exit(1)

    if not is_admin():
        sys.exit(1) # Без админа мы всё равно не сможем сбросить DNS

    # Простой цикл: проверяем жив ли PID через tasklist /FI (работает без psutil)
    cmd = f'tasklist /FI "PID eq {parent_pid}" /NH'
    
    while True:
        time.sleep(3)
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            # Если tasklist пишет "No tasks" или "не найдены" — процесс мертв
            out = res.stdout.lower()
            if "no tasks" in out or "не найдены" in out or str(parent_pid) not in out:
                # Опа! Программа упала. Спасаем интернет!
                reset_dns_to_auto()
                break
        except Exception:
            # При любой ошибке проверки лучше сбросить DNS от греха подальше
            reset_dns_to_auto()
            break

if __name__ == "__main__":
    main()
