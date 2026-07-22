"""
UmbraNet — Монитор процессов + управление DNS Windows
"""

import psutil
import time
import threading
import logging
import os
import sys
import subprocess

log = logging.getLogger("UmbraNet.ProcessMonitor")

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False
    else:
        return os.geteuid() == 0


def get_running_processes() -> list:
    procs = []
    # Забираем только pid и name, потому что exe требует прав админа 
    # на многие процессы и вызов WMI работает очень долго, вызывая зависания UI!
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            procs.append({
                'pid': proc.info['pid'],
                'name': proc.info['name']
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def is_process_running(process_name: str) -> bool:
    name_lower = process_name.lower()
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == name_lower:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _console_oem_codepage() -> str:
    """
    Возвращает кодовую страницу, в которой консольные утилиты Windows
    (netsh, powershell legacy host) печатают вывод.

    На русской Windows это обычно cp866 (OEM/DOS), а НЕ cp1251 и НЕ utf-8.
    Если вывод декодировать неправильно, текст ошибки превращается в
    «кракозябры» (напр. 'ЌҐ г¤Ґвбп ...'), и парсер по слову 'OK' решает,
    что всё прошло успешно, хотя на деле была ошибка.

    Берём реальную console output codepage через WinAPI, чтобы фикс работал
    не только на русской локали, но и на любой другой (cp850, cp437 и т.п.).
    Фолбэк — cp866 (самый частый случай для RU), затем utf-8.
    """
    if not IS_WINDOWS:
        return 'utf-8'
    try:
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        if cp:
            return f'cp{cp}'
    except Exception:
        pass
    return 'cp866'


def _decode_console(data: bytes) -> str:
    """Безопасно декодирует сырой вывод консольной утилиты Windows."""
    if data is None:
        return ''
    if isinstance(data, str):
        return data
    for enc in (_console_oem_codepage(), 'cp866', 'utf-8'):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # Последняя линия обороны — не падаем, заменяем нечитаемое.
    return data.decode('utf-8', errors='replace')


def _run_ps(command: str, timeout: int = 15) -> tuple:
    """
    Запускает PowerShell команду без окна консоли.
    Возвращает (stdout, stderr, returncode).

    ВАЖНО: вывод читаем сырыми байтами (без text=True) и декодируем сами
    через _decode_console(), потому что text=True использует локаль Python
    (часто utf-8) и ломает русский вывод netsh/powershell (он в cp866).

    Двойная защита от появления окна консоли:
      1. CREATE_NO_WINDOW (0x08000000) — флаг создания процесса
      2. STARTUPINFO с SW_HIDE — скрывает окно даже если (1) не сработал
         (актуально при запуске через python.exe вместо pythonw.exe)
    """
    flags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW

    # STARTUPINFO со SW_HIDE — дополнительная защита от мелькания окна
    startupinfo = None
    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive',
             '-ExecutionPolicy', 'Bypass', '-Command', command],
            capture_output=True, timeout=timeout,
            creationflags=flags,
            startupinfo=startupinfo,
        )
        stdout = _decode_console(result.stdout).strip()
        stderr = _decode_console(result.stderr).strip()
        return stdout, stderr, result.returncode
    except FileNotFoundError:
        return '', 'PowerShell not found', -1
    except subprocess.TimeoutExpired:
        return '', 'Timeout', -2
    except Exception as e:
        return '', str(e), -3


def get_active_adapters() -> list:
    """Возвращает список имён активных сетевых адаптеров."""
    stdout, stderr, code = _run_ps(
        "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object -ExpandProperty Name | ForEach-Object { $_ }"
    )
    if code != 0 or not stdout:
        log.warning(f"get_active_adapters error: {stderr}")
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def get_current_dns() -> dict:
    """
    Возвращает текущие DNS для всех адаптеров (IPv4 и IPv6).
    Формат: {'AdapterName': {'ipv4': [...], 'ipv6': [...]}}
    """
    result = {}

    active_filter = (
        "$up = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object -ExpandProperty Name; "
        "$skip = 'loopback|isatap|teredo|bluetooth|pseudo|virtual|wsl|vethernet|tunnel|виртуальн'; "
    )

    # IPv4
    stdout4, _, _ = _run_ps(
        active_filter +
        "Get-DnsClientServerAddress -AddressFamily IPv4 | "
        "Where-Object {$_.ServerAddresses.Count -gt 0 -and $up -contains $_.InterfaceAlias -and $_.InterfaceAlias -notmatch $skip} | "
        "ForEach-Object { $_.InterfaceAlias + '|' + ($_.ServerAddresses -join ',') }"
    )
    if stdout4:
        for line in stdout4.splitlines():
            line = line.strip()
            if '|' in line:
                name, ips = line.split('|', 1)
                name = name.strip()
                if name not in result:
                    result[name] = {'ipv4': [], 'ipv6': []}
                result[name]['ipv4'] = [ip.strip() for ip in ips.split(',') if ip.strip()]

    # IPv6
    stdout6, _, _ = _run_ps(
        active_filter +
        "Get-DnsClientServerAddress -AddressFamily IPv6 | "
        "Where-Object {$_.ServerAddresses.Count -gt 0 -and $up -contains $_.InterfaceAlias -and $_.InterfaceAlias -notmatch $skip} | "
        "ForEach-Object { $_.InterfaceAlias + '|' + ($_.ServerAddresses -join ',') }"
    )
    if stdout6:
        for line in stdout6.splitlines():
            line = line.strip()
            if '|' in line:
                name, ips = line.split('|', 1)
                name = name.strip()
                if name not in result:
                    result[name] = {'ipv4': [], 'ipv6': []}
                result[name]['ipv6'] = [ip.strip() for ip in ips.split(',') if ip.strip()]

    return result


# ── xbox-dns.ru — Smart DNS прокси ───────────────────────────────────────────
#
# Как работает:
#   Вместо настоящего IP chatgpt.com (104.18.x.x) xbox-dns.ru возвращает
#   IP СВОЕГО прокси-сервера (87.228.47.x). Браузер подключается к их серверу,
#   тот перенаправляет трафик на ChatGPT. ChatGPT видит IP xbox-dns.ru → работает!
#
# Способы подключения:
#   1. Системный DNS (IPv4): см. XBOX_DNS_PRIMARY — работает для ВСЕХ программ
#   2. DoH (браузер): https://xbox-dns.ru/dns-query — только для браузера
#
# Для ПК рекомендуется способ 1 — системный DNS, тогда работает всё!

# Адреса берём из единого источника правды (profile_utils), чтобы при смене
# IP сервиса не править их в нескольких файлах.
from profile_utils import (
    XBOX_DNS_IPV4_PRIMARY as XBOX_DNS_PRIMARY,
    XBOX_DNS_IPV4_SECONDARY as XBOX_DNS_SECONDARY,
    XBOX_DNS_IPV6_PRIMARY as XBOX_DNS_IPV6_PRI,
    XBOX_DNS_IPV6_SECONDARY as XBOX_DNS_IPV6_SEC,
    XBOX_DOH_URL,
)
XBOX_DOH_NAME       = 'xbox-dns.ru'

# Фейковые IP которые МТС подставляет вместо настоящих
MTS_FAKE_IPS = {'8.6.112.0', '8.47.69.0', '8.43.85.0', '8.43.85.1', '8.34.212.0'}


def set_dns_profile(ipv4_primary: str, ipv4_secondary: str = "",
                    ipv6_primary: str = "", ipv6_secondary: str = "",
                    profile_name: str = "DNS-профиль") -> tuple:
    """Устанавливает системный DNS на указанный профиль (IPv4 + IPv6)."""
    if not IS_WINDOWS:
        return False, "Только Windows", []
    if not is_admin():
        return False, "Требуются права администратора", []

    ipv4_list = [ip for ip in (ipv4_primary, ipv4_secondary) if ip]
    ipv6_list = [ip for ip in (ipv6_primary, ipv6_secondary) if ip]
    if not ipv4_list and not ipv6_list:
        return False, "Профиль не содержит ни одного DNS-адреса", []

    ipv4_ps = "@(" + ",".join(f"'{v}'" for v in ipv4_list) + ")" if ipv4_list else ""
    ipv6_ps = "@(" + ",".join(f"'{v}'" for v in ipv6_list) + ")" if ipv6_list else ""

    ipv4_action = (
        "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        f"-ServerAddresses {ipv4_ps}; "
    ) if ipv4_list else (
        "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        "-ResetServerAddresses; "
    )

    # ВАЖНО: у Set-DnsClientServerAddress НЕТ параметра -AddressFamily —
    # семейство определяется по самим адресам. Для IPv6 передаём IPv6-адреса.
    # Для сброса ТОЛЬКО IPv6 используем netsh (Reset сбрасывает оба семейства).
    ipv6_action = (
        "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        f"-ServerAddresses {ipv6_ps}; "
    ) if ipv6_list else (
        "netsh interface ipv6 set dnsservers \"$($a.Name)\" source=dhcp | Out-Null; "
    )

    ps_cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "$ok = @(); $err = @(); "
        "foreach ($a in $adapters) { "
        "  try { "
        f"    {ipv4_action}"
        f"    {ipv6_action}"
        "    $ok += $a.Name "
        "  } catch { "
        "    $err += $a.Name + ': ' + $_.Exception.Message "
        "  } "
        "}; "
        "Clear-DnsClientCache; "
        "Write-Output ('OK:' + ($ok -join ',')); "
        "if ($err) { Write-Output ('ERR:' + ($err -join ';')) }"
    )

    stdout, stderr, _code = _run_ps(ps_cmd, timeout=25)
    log.info(f"set_dns_profile stdout: {stdout!r}")

    ok_adapters, err_adapters = [], []
    for line in stdout.splitlines():
        if line.startswith('OK:'):
            ok_adapters = [x for x in line[3:].split(',') if x]
        elif line.startswith('ERR:'):
            err_adapters = [x for x in line[4:].split(';') if x]

    if ok_adapters:
        msg = f"{profile_name} на: {', '.join(ok_adapters)}"
        if err_adapters:
            msg += f"\nОшибки: {'; '.join(err_adapters)}"
        return True, msg, ok_adapters
    return False, f"Не удалось: {stderr}", []


def set_dns_xbox() -> tuple:
    return set_dns_profile(
        XBOX_DNS_PRIMARY,
        XBOX_DNS_SECONDARY,
        XBOX_DNS_IPV6_PRI,
        XBOX_DNS_IPV6_SEC,
        profile_name="xbox-dns.ru",
    )


def set_chrome_doh(doh_url: str = XBOX_DOH_URL) -> tuple:
    """
    Прописывает DoH-провайдер в Chrome через реестр Windows.
    Chrome будет использовать указанный DoH для ВСЕХ DNS-запросов,
    независимо от системного DNS.
    Возвращает (success: bool, message: str)
    """
    if not IS_WINDOWS:
        return False, "Только Windows"

    # Chrome хранит настройки в реестре
    # HKLM\SOFTWARE\Policies\Google\Chrome  (для всех пользователей, нужны права)
    # HKCU\SOFTWARE\Policies\Google\Chrome  (для текущего пользователя, без прав)
    ps_cmd = (
        "$url = '" + doh_url + "'; "
        # Пробуем HKCU (не нужны права администратора)
        "$path = 'HKCU:\\SOFTWARE\\Policies\\Google\\Chrome'; "
        "if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsMode' -Value 'secure' -Type String; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsTemplates' -Value $url -Type String; "
        # Также пробуем HKLM если есть права
        "if ([bool](([System.Security.Principal.WindowsIdentity]::GetCurrent()).groups "
        "    -match 'S-1-5-32-544')) { "
        "  $path2 = 'HKLM:\\SOFTWARE\\Policies\\Google\\Chrome'; "
        "  if (-not (Test-Path $path2)) { New-Item -Path $path2 -Force | Out-Null }; "
        "  Set-ItemProperty -Path $path2 -Name 'DnsOverHttpsMode' -Value 'secure' -Type String; "
        "  Set-ItemProperty -Path $path2 -Name 'DnsOverHttpsTemplates' -Value $url -Type String "
        "}; "
        "Write-Output 'OK'"
    )

    stdout, stderr, code = _run_ps(ps_cmd, timeout=15)
    log.info(f"set_chrome_doh: {stdout!r} {stderr!r}")

    if 'OK' in stdout:
        return True, f"DoH прописан в Chrome: {doh_url}\nПерезапусти Chrome для применения."
    else:
        return False, f"Ошибка записи в реестр: {stderr}"


def set_edge_doh(doh_url: str = XBOX_DOH_URL) -> tuple:
    """
    Прописывает DoH-провайдер в Microsoft Edge через реестр.
    """
    if not IS_WINDOWS:
        return False, "Только Windows"

    ps_cmd = (
        "$url = '" + doh_url + "'; "
        "$path = 'HKCU:\\SOFTWARE\\Policies\\Microsoft\\Edge'; "
        "if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsMode' -Value 'secure' -Type String; "
        "Set-ItemProperty -Path $path -Name 'DnsOverHttpsTemplates' -Value $url -Type String; "
        "Write-Output 'OK'"
    )

    stdout, stderr, _ = _run_ps(ps_cmd, timeout=15)
    if 'OK' in stdout:
        return True, f"DoH прописан в Edge: {doh_url}"
    else:
        return False, f"Ошибка: {stderr}"


def reset_chrome_doh() -> tuple:
    """Убирает DoH-политику Chrome из реестра."""
    if not IS_WINDOWS:
        return False, "Только Windows"

    # Используем отдельные команды для HKCU и HKLM — проще и надёжнее
    ps_cmd = (
        "$paths = @("
        "  'HKCU:\\SOFTWARE\\Policies\\Google\\Chrome',"
        "  'HKLM:\\SOFTWARE\\Policies\\Google\\Chrome'"
        "); "
        "foreach ($p in $paths) { "
        "  if (Test-Path $p) { "
        "    Remove-ItemProperty -Path $p -Name 'DnsOverHttpsMode' -ErrorAction SilentlyContinue; "
        "    Remove-ItemProperty -Path $p -Name 'DnsOverHttpsTemplates' -ErrorAction SilentlyContinue "
        "  } "
        "}; "
        "Write-Output 'OK'"
    )
    stdout, stderr, _ = _run_ps(ps_cmd, timeout=10)
    if 'OK' in stdout:
        return True, "DoH политика Chrome удалена. Перезапусти Chrome."
    return False, f"Ошибка: {stderr}"


def set_dns_to_localhost(fallback_ipv4: str = '1.1.1.1',
                         fallback_ipv6: str = '2606:4700:4700::1111',
                         enable_ipv6: bool = True) -> tuple:
    """
    Устанавливает системный DNS на локальный UmbraNet и запасные серверы из конфигурации.

    IPv4:
      - основной: 127.0.0.1
      - резерв: fallback_ipv4

    IPv6:
      - если enable_ipv6=True:
          только ::1 (без внешнего fallback, иначе возможна IPv6 DNS-утечка)
      - если enable_ipv6=False:
          ставим только fallback_ipv6, либо сбрасываем IPv6 DNS на авто

    Возвращает (success: bool, message: str, adapters: list)
    """
    if not IS_WINDOWS:
        return False, "Только Windows", []
    if not is_admin():
        return False, "Требуются права администратора", []

    ipv4_servers = ['127.0.0.1']
    if fallback_ipv4 and fallback_ipv4 not in ipv4_servers:
        ipv4_servers.append(fallback_ipv4)

    ipv6_servers = []
    if enable_ipv6:
        # ВАЖНО: для IPv6 не ставим внешний fallback рядом с ::1.
        # Windows может уйти на второй IPv6-DNS провайдера/Google, и это будет
        # реальная IPv6 DNS-утечка мимо UmbraNet. Если локальный ::1 не работает,
        # лучше явно показать проблему, а не тихо протечь наружу.
        ipv6_servers = ['::1']
    elif fallback_ipv6:
        ipv6_servers = [fallback_ipv6]

    ipv4_ps = "@(" + ",".join(f"'{v}'" for v in ipv4_servers) + ")"
    ipv6_ps = "@(" + ",".join(f"'{v}'" for v in ipv6_servers) + ")" if ipv6_servers else ""
    if ipv6_servers:
        # Set-DnsClientServerAddress определяет семейство по адресам (IPv6 → IPv6).
        # Параметра -AddressFamily у Set- НЕТ (он есть только у Get-).
        ipv6_action = (
            "Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
            f"-ServerAddresses {ipv6_ps}; "
        )
    else:
        # сброс ТОЛЬКО IPv6-DNS (через netsh, т.к. -ResetServerAddresses сбросил бы оба)
        ipv6_action = (
            "netsh interface ipv6 set dnsservers \"$($a.Name)\" source=dhcp | Out-Null; "
        )

    ps_cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "$ok = @(); $err = @(); "
        "foreach ($a in $adapters) { "
        "  try { "
        "    Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex "
        f"      -ServerAddresses {ipv4_ps}; "
        f"    {ipv6_action}"
        "    $ok += $a.Name "
        "  } catch { "
        "    $err += $a.Name + ': ' + $_.Exception.Message "
        "  } "
        "}; "
        "Clear-DnsClientCache; "
        "Write-Output ('OK:' + ($ok -join ',')); "
        "if ($err) { Write-Output ('ERR:' + ($err -join ';')) }"
    )

    stdout, stderr, code = _run_ps(ps_cmd, timeout=20)
    log.info(f"set_dns_to_localhost: {stdout!r}")

    ok_adapters, err_adapters = [], []
    for line in stdout.splitlines():
        if line.startswith('OK:'):
            ok_adapters = [x for x in line[3:].split(',') if x]
        elif line.startswith('ERR:'):
            err_adapters = [x for x in line[4:].split(';') if x]

    if ok_adapters:
        msg = (
            f"DNS → {ipv4_servers[0]} / "
            f"IPv4 резерв: {fallback_ipv4 or 'нет'} / "
            f"IPv6: {', '.join(ipv6_servers) if ipv6_servers else 'авто'}\n"
            f"Адаптеры: {', '.join(ok_adapters)}"
        )
        if err_adapters:
            msg += f"\nОшибки: {'; '.join(err_adapters)}"
        return True, msg, ok_adapters

    # Ни один адаптер не настроен. Покажем человекочитаемую причину:
    # сначала разобранные ERR-строки (теперь они в правильной кодировке),
    # затем stderr PowerShell, и только потом общий текст.
    if err_adapters:
        reason = '; '.join(err_adapters)
    elif stderr:
        reason = stderr
    else:
        reason = "ни один сетевой адаптер не удалось настроить"
    return False, f"Не удалось: {reason}", []


def reset_dns_to_auto() -> tuple:
    """Сбрасывает DNS на DHCP (IPv4 и IPv6)."""
    if not IS_WINDOWS:
        return False, "Только Windows"
    if not is_admin():
        return False, "Требуются права администратора"

    ps_cmd = (
        "$adapters = Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}; "
        "$ok = @(); "
        "foreach ($a in $adapters) { "
        "  Set-DnsClientServerAddress -InterfaceIndex $a.InterfaceIndex -ResetServerAddresses; "
        "  $ok += $a.Name "
        "}; "
        "Clear-DnsClientCache; "
        "Write-Output ('OK:' + ($ok -join ','))"
    )

    stdout, stderr, _ = _run_ps(ps_cmd, timeout=20)
    for line in stdout.splitlines():
        if line.startswith('OK:'):
            adapters = [x for x in line[3:].split(',') if x]
            return True, f"DNS сброшен на DHCP: {', '.join(adapters)}"
    return False, f"Ошибка: {stderr}"


def flush_dns_cache() -> bool:
    """Очищает DNS-кэш Windows."""
    if not IS_WINDOWS:
        return False
    _, _, code = _run_ps("Clear-DnsClientCache")
    return code == 0


class ProcessMonitor:
    def __init__(self, callback=None):
        self.callback = callback
        self.running = False
        self._thread = None
        self._known_pids = set()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _monitor_loop(self):
        while self.running:
            try:
                current_pids = set()
                for proc in psutil.process_iter(['pid']):
                    try:
                        current_pids.add(proc.info['pid'])
                    except Exception:
                        continue

                if (current_pids != self._known_pids) and self.callback:
                    self.callback(get_running_processes())

                self._known_pids = current_pids
            except Exception as e:
                log.error(f"ProcessMonitor error: {e}")
            time.sleep(3)
