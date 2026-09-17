"""
UmbraNet — единая точка общения с системой Windows (пункт H4).
================================================================================

**Зачем модуль.** Раньше PowerShell вызывался из четырёх мест (`process_monitor`,
`dpi/winws_engine`, `process_dns_tracker`, `network_repair`), и у каждого была
своя обработка отказа: где-то пустая строка, где-то `False`, где-то исключение.
Если PowerShell в системе недоступен — а это живой случай: политика компании,
антивирус, вырезанный Windows PowerShell, отключённый групповой политикой, — то
программа вела себя по-разному в разных местах: где-то «ничего не работает и
непонятно почему», где-то молча ничего не делала. Пользователь видел «функция
сломана» вместо «PowerShell заблокирован, вот что делать».

**Что здесь есть.**

1. **Живая проверка доступности** — `powershell_available()`. Один дешёвый запуск
   (`Write-Output`), результат кэшируется на `PROBE_TTL` секунд и перепроверяется:
   антивирус может отпустить PowerShell после обновления баз, и программа обязана
   это заметить, а не работать «в деградации» до перезапуска.
2. **Деградация на системные утилиты** — `flush_dns_cache()`, `list_adapters()`,
   `list_dns_servers()`, `set_dns_servers()`, `reset_dns_servers()`,
   `kill_process()`, `reg_set()` / `reg_delete()`. Если PowerShell нет, те же
   действия выполняются через `netsh`, `ipconfig`, `taskkill`, `reg` — они есть в
   любой Windows и блокируются антивирусом куда реже.
3. **Внятная ошибка** — `human_error()` и `status_line()`: готовый текст для
   интерфейса и отчёта диагностики — что случилось, что работает, что делать.

**Чего здесь нет.** Модуль не знает про устройство UmbraNet: только про систему.
Логика «когда ставить 127.0.0.1» остаётся в `process_monitor`.

**Тесты** (`tests/test_win_shell.py`) подменяют `_run_hidden` — единственное место,
где модуль запускает процессы, — поэтому проверяются и «PowerShell есть», и
«PowerShell заблокирован», без Windows.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger("UmbraNet.WinShell")

IS_WINDOWS = sys.platform == "win32"

# Переменная среды: путь к powershell.exe. Нужна и тестам, и человеку с
# нестандартной установкой (Windows PowerShell вырезан, есть только PowerShell 7).
POWERSHELL_ENV = "UMBRANET_POWERSHELL"

# Сколько секунд верить результату проверки доступности. Меньше — чаще дёргаем
# систему (а каждый запуск PowerShell стоит 0.3–1 с), больше — дольше не замечаем,
# что антивирус разблокировал PowerShell.
PROBE_TTL = 300.0

# Дешёвый признак «PowerShell жив»: печать строки. Ничего не читает и не меняет.
PROBE_CMD = "Write-Output 'UMBRANET-PS-OK'"
PROBE_MARK = "UMBRANET-PS-OK"
PROBE_TIMEOUT = 8.0

CREATE_NO_WINDOW = 0x08000000        # не мелькать окном консоли

# Почему PowerShell может быть недоступен — человеческим языком.
REASONS = {
    "ok": "доступен",
    "missing": "powershell.exe не найден",
    "not_windows": "это не Windows",
    "blocked": "запуск заблокирован (антивирус или политика)",
    "timeout": "не отвечает (заблокирован антивирусом или завис)",
    "error": "ошибка запуска",
}

# Что именно недоступно без PowerShell — короткими фразами: их читает человек в
# предупреждении на вкладке и в строке отчёта. Длинные пояснения — в `limitations()`.
LIMIT_SHORT = {
    "log": "журнал DNS-запросов по событиям Windows",
    "repair": "автопочинка сети и восстановление DNS из снапшота",
    "leak": "проверка утечек IPv6 по адресам адаптеров",
}

POWERSHELL_HINT = (
    "Разрешите powershell.exe в антивирусе или в политике безопасности — UmbraNet "
    "подхватит это без перезапуска."
)


def human_error(reason: str) -> str:
    """Короткая причина для логов и сообщений: одна строка, без «что делать».

    Подсказку (что делать) добавляет `status_line()` / `POWERSHELL_HINT`: в логе
    четыреста символов на каждое предупреждение не нужно, а в интерфейсе —
    наоборот, без неё человек остаётся один на один с «не работает».
    """
    text = REASONS.get(reason, REASONS["error"])
    if reason == "ok":
        return ""
    if reason == "not_windows":
        return "PowerShell недоступен: это не Windows"
    return f"PowerShell недоступен: {text}"


# ── декодирование вывода консоли ────────────────────────────────────────────
# Windows-утилиты (netsh, ipconfig, powershell legacy host) печатают в кодовой
# странице консоли (cp866 на русской Windows), а не в utf-8. Если читать
# text=True, Python возьмёт локаль и сломает русский текст.

def _console_oem_codepage() -> str:
    """Кодовая страница консоли: на русской Windows это cp866, а не cp1251."""
    if not IS_WINDOWS:
        return "utf-8"
    try:
        cp = int(ctypes.windll.kernel32.GetOEMCP())
    except (AttributeError, OSError, ValueError) as exc:
        log.debug("Кодовая страница консоли не определена (%s) — берём cp866", exc)
        return "cp866"
    return f"cp{cp}" if cp else "cp866"


OEM_CODEPAGE = _console_oem_codepage()


def decode_console(data: bytes) -> str:
    """Байты вывода консольной программы → текст (cp866, затем cp1251, затем utf-8)."""
    if not data:
        return ""
    for enc in (OEM_CODEPAGE, "cp866", "cp1251", "utf-8"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # Последняя линия обороны — не падаем, заменяем нечитаемое.
    return data.decode("utf-8", errors="replace")


@dataclass
class ShellResult:
    """Результат системного вызова: что вышло, чем выполнено и почему не вышло."""

    stdout: str = ""
    stderr: str = ""
    code: int = 0
    via: str = "powershell"          # powershell / netsh / ipconfig / taskkill / reg / none
    human_error: str = ""            # готовый текст для человека (пусто = всё хорошо)
    note: str = ""                   # почему сработало не через PowerShell
    parts: list[str] = field(default_factory=list)   # пояснения по шагам (для отчёта)

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.human_error

    def text(self, fallback: str = "") -> str:
        return self.stdout or self.stderr or fallback


# ── низкий уровень: запуск скрытого процесса ────────────────────────────────
# Единственное место в модуле, где запускаются процессы: тесты подменяют именно
# эту функцию и проверяют и «PowerShell есть», и «PowerShell заблокирован».

def _run_hidden(args, timeout: float = 15.0) -> tuple[str, str, int]:
    """Запускает программу без окна консоли, возвращает (stdout, stderr, код)."""
    flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
    startupinfo = None
    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        proc = subprocess.run(
            list(args), capture_output=True, timeout=timeout, check=False,
            creationflags=flags, startupinfo=startupinfo,
        )
    except FileNotFoundError:
        return "", "программа не найдена", -1
    except PermissionError as exc:
        return "", f"запуск запрещён: {exc}", -4
    except subprocess.TimeoutExpired:
        return "", f"таймаут {timeout:g} с", -2
    except OSError as exc:
        return "", str(exc), -3
    return (decode_console(proc.stdout).strip(),
            decode_console(proc.stderr).strip(),
            int(proc.returncode))


# ── проверка доступности PowerShell ─────────────────────────────────────────

_probe: dict = {"checked": False, "ts": 0.0, "available": False, "path": "", "reason": "error"}


def powershell_candidates() -> list[str]:
    """Где искать powershell.exe: переменная среды, затем системная папка, затем PATH."""
    env_path = (os.environ.get(POWERSHELL_ENV) or "").strip()
    if env_path:
        return [env_path]
    if not IS_WINDOWS:
        return []
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return [
        os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "powershell.exe",
        "pwsh.exe",
    ]


def _probe_powershell() -> dict:
    """Один дешёвый запуск: жив ли PowerShell. Возвращает запись для кэша."""
    now = time.monotonic()
    if not IS_WINDOWS:
        return {"checked": True, "ts": now, "available": False, "path": "",
                "reason": "not_windows"}

    last_reason = "missing"
    for exe in powershell_candidates():
        out, err, code = _run_hidden(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", PROBE_CMD],
            timeout=PROBE_TIMEOUT,
        )
        if code == 0 and PROBE_MARK in out:
            return {"checked": True, "ts": now, "available": True, "path": exe,
                    "reason": "ok"}
        # Разбираем причину, чтобы человеку достался внятный текст, а не «не работает».
        if code == -1:
            last_reason = "missing"
            continue
        if code == -4:
            last_reason = "blocked"
        elif code == -2:
            last_reason = "timeout"
        elif "запуск запрещён" in err or "access is denied" in err.lower():
            last_reason = "blocked"
        elif err:
            last_reason = "error"
    return {"checked": True, "ts": now, "available": False, "path": "",
            "reason": last_reason}


def powershell_status(force: bool = False) -> dict:
    """Статус PowerShell с кэшем: {'available', 'path', 'reason', 'checked'}."""
    fresh = _probe.get("checked") and (time.monotonic() - _probe.get("ts", 0.0)) < PROBE_TTL
    if force or not fresh:
        probe = _probe_powershell()
        _probe.update(probe)
        if probe["available"]:
            log.debug("PowerShell доступен: %s", probe["path"])
        else:
            log.info("PowerShell недоступен (%s) — работаем через системные утилиты",
                     REASONS.get(probe["reason"], probe["reason"]))
    return dict(_probe)


def powershell_available(force: bool = False) -> bool:
    return bool(powershell_status(force=force)["available"])


def invalidate_probe() -> None:
    """Забыть результат проверки: следующий вызов проверит заново.

    Нужно, когда запуск PowerShell провалился уже после успешной проверки —
    значит антивирус (или политика) подключился и ударил по нему.
    """
    _probe["checked"] = False
    _probe["ts"] = 0.0


def reset_probe() -> None:
    """Полный сброс кэша проверки (тесты и смена настроек окружения)."""
    _probe.update({"checked": False, "ts": 0.0, "available": False,
                   "path": "", "reason": "error"})


# ── запуск PowerShell и системных утилит ────────────────────────────────────

def run_ps(command: str, timeout: float = 15.0) -> ShellResult:
    """Одна команда PowerShell. Если PowerShell недоступен — не запускаем вовсе."""
    if not IS_WINDOWS:
        return ShellResult(code=-1, via="none", human_error=human_error("not_windows"))
    status = powershell_status()
    if not status["available"]:
        return ShellResult(code=-1, via="none",
                           human_error=human_error(status.get("reason", "error")))
    exe = status.get("path") or "powershell.exe"
    out, err, code = _run_hidden(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", command],
        timeout=timeout,
    )
    if code in (-1, -4):
        # Его не стало между проверками — пусть следующий вызов проверит заново.
        invalidate_probe()
    return ShellResult(stdout=out, stderr=err, code=code, via="powershell",
                       human_error=("" if code == 0 else (err or f"код {code}")))


def run_system(args, timeout: float = 15.0) -> ShellResult:
    """Системная утилита (netsh / ipconfig / taskkill / reg) — без PowerShell."""
    if not IS_WINDOWS:
        return ShellResult(code=-1, via="none", human_error=human_error("not_windows"))
    out, err, code = _run_hidden(list(args), timeout=timeout)
    via = os.path.basename(str(args[0])).lower().removesuffix(".exe")
    return ShellResult(stdout=out, stderr=err, code=code, via=via,
                       human_error=("" if code == 0 else f"{via}: {err or f'код {code}'}"))


def _fallback(ps_result: ShellResult, native_result: ShellResult,
              native_result_sink: list | None = None) -> ShellResult:
    """Собирает итог деградации: результат утилиты плюс пометка, почему не PowerShell.

    Если не сработало ни то, ни другое — человеку нужны ОБЕ причины (иначе он
    чинит не то). Но один и тот же текст дважды не показываем: на Linux, например,
    обе причины — «это не Windows».
    """
    reasons = []
    for text in (ps_result.human_error or ps_result.stderr,
                 native_result.human_error or native_result.stderr):
        text = (text or "").strip().rstrip(".")
        if text and text not in reasons:
            reasons.append(text)
    if native_result.ok:
        native_result.note = "; ".join(reasons)
        native_result.parts = list(reasons)
    else:
        native_result.human_error = ". ".join(reasons) if reasons else "не сработало"
    if native_result_sink is not None:
        native_result_sink.append(native_result.note or "")
    return native_result


# ── готовые операции: каждая умеет деградировать ────────────────────────────

def flush_dns_cache() -> ShellResult:
    """Очистка DNS-кэша: `Clear-DnsClientCache` → `ipconfig /flushdns`."""
    ps = run_ps("Clear-DnsClientCache")
    if ps.ok:
        return ps
    native = run_system(["ipconfig", "/flushdns"], timeout=20)
    return _fallback(ps, native)


def list_adapters() -> tuple[list[str], ShellResult]:
    """Активные сетевые адаптеры: `Get-NetAdapter` → `netsh interface show interface`."""
    ps = run_ps("Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
                "Select-Object -ExpandProperty Name")
    if ps.ok:
        names = [line.strip() for line in ps.stdout.splitlines() if line.strip()]
        return names, ps

    native = run_system(["netsh", "interface", "show", "interface"], timeout=15)
    names = _parse_netsh_interfaces(native.stdout)
    if names:
        return names, _fallback(ps, native)
    if not native.ok:
        return [], _fallback(ps, native)
    # netsh отработал, но активных адаптеров нет — это честный пустой ответ.
    native.note = "; ".join([ps.human_error or ps.stderr]).strip()
    return [], native


def _parse_netsh_interfaces(text: str) -> list[str]:
    """Разбирает вывод `netsh interface show interface` → имена подключённых адаптеров.

    Вывод локализован (на русской Windows «Включено / Подключено / Выделенный»), а
    имя адаптера может содержать пробелы. Поэтому режем по двум и более пробелам:
    первые три колонки служебные, остаток строки — имя. Отключённые (в том числе
    административно) пропускаем — их DNS роли не играет.
    """
    skip_states = {"отключено", "disconnected", "disabled", "не подключено"}
    names: list[str] = []
    for raw in (text or "").splitlines():
        parts = re.split(r"\s{2,}", raw.strip())
        if len(parts) < 4:
            continue                       # заголовок таблицы и мусор
        admin, state = parts[0].strip().lower(), parts[1].strip().lower()
        if admin in skip_states or state in skip_states:
            continue
        name = " ".join(parts[3:]).strip()
        if name and not name.lower().startswith(("имя", "interface", "name")):
            names.append(name)
    return names


_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6 собираем цельным: «::1», «2a00:1450:4010::1», «fe80::1%12» — сокращённые
# адреса («::») предыдущий шаблон обрезал до «2a00:1450:4010», и сравнение адресов
# в проверке утечек работало бы неправильно.
_IPV6_RE = re.compile(r"[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,}(?:%[0-9]+)?", re.IGNORECASE)


# Служебные адаптеры: их DNS не влияет на интернет, зато путает проверку утечек.
# Список совпадает с фильтром в PowerShell-команде чтения DNS — иначе netsh-путь
# приносил бы DNS виртуальных адаптеров и показывал утечку там, где её нет.
SKIP_ADAPTER_KEYWORDS = (
    "loopback", "isatap", "teredo", "bluetooth", "pseudo", "virtual",
    "wsl", "vethernet", "tunnel", "виртуальн",
)


def is_service_adapter(name: str) -> bool:
    """True для служебных/виртуальных адаптеров (loopback, WSL, Hyper-V и т.п.)."""
    low = (name or "").lower()
    return any(kw in low for kw in SKIP_ADAPTER_KEYWORDS)


def parse_dns_servers(text: str) -> tuple[list[str], list[str]]:
    """Вытаскивает IPv4 и IPv6-адреса из вывода `netsh ... show dnsservers`.

    В этом выводе других адресов, кроме DNS, не бывает — поэтому разбор идёт по
    самим адресам, а не по словам. Это важно: на русской и английской Windows
    подписи разные («DNS-серверы» / «DNS Servers»), а адреса одинаковые.
    Порядок сохраняем: первый адрес — основной.
    """
    ipv4: list[str] = []
    ipv6: list[str] = []
    for raw in (text or "").splitlines():
        for ip in _IPV4_RE.findall(raw):
            if ip not in ipv4:
                ipv4.append(ip)
        for ip in _IPV6_RE.findall(raw):
            if ip not in ipv6:
                ipv6.append(ip)
    return ipv4, ipv6


def dns_servers_native(adapters: list[str] | None = None) -> tuple[dict, ShellResult]:
    """DNS-серверы адаптеров через netsh — когда PowerShell недоступен.

    Возвращает ({адаптер: {'ipv4': [...], 'ipv6': [...]}}, результат) — тот же
    формат, что даёт PowerShell-путь, поэтому вызывающему коду не приходится
    знать, каким путём получены данные.
    """
    if not IS_WINDOWS:
        return {}, ShellResult(code=-1, via="none", human_error=human_error("not_windows"))
    if adapters is None:
        adapters, _res = list_adapters()
    data: dict = {}
    errors: list[str] = []
    for name in [a for a in adapters if not is_service_adapter(a)]:
        for family in ("ipv4", "ipv6"):
            res = run_system(
                ["netsh", "interface", family, "show", "dnsservers", f"name={name}"],
                timeout=10,
            )
            if not res.ok:
                errors.append(f"{name} ({family}): {res.human_error}")
                continue
            ipv4, ipv6 = parse_dns_servers(res.stdout)
            found = ipv4 if family == "ipv4" else ipv6
            if found:
                data.setdefault(name, {"ipv4": [], "ipv6": []})[family] = found
    result = ShellResult(stdout=f"{len(data)} адаптер(ов)", code=(0 if (data or not errors) else -5),
                         via="netsh", human_error=("" if (data or not errors)
                                                   else "; ".join(errors[:3])),
                         parts=errors[:5])
    return data, result


def set_dns_servers(adapter: str, ipv4: list[str] | None = None,
                    ipv6: list[str] | None = None,
                    reset_ipv4: bool = False, reset_ipv6: bool = False) -> ShellResult:
    """Ставит статический DNS адаптеру через netsh (путь без PowerShell).

    reset_ipv4 / reset_ipv6 — вернуть это семейство на DHCP: нужно там, где по
    IPv6 внешний DNS ставить нельзя (будет утечка мимо UmbraNet), а оставлять
    старый чужой адрес тоже нельзя.
    """
    if not IS_WINDOWS:
        return ShellResult(code=-1, via="none", human_error=human_error("not_windows"))
    parts: list[str] = []
    errors: list[str] = []
    quoted = f'name="{adapter}"'
    if reset_ipv4 and not ipv4:
        res = run_system(["netsh", "interface", "ipv4", "set", "dnsservers",
                          quoted, "source=dhcp"], timeout=15)
        (parts if res.ok else errors).append(
            f"{adapter} (ipv4): авто" if res.ok else f"{adapter} (ipv4): {res.human_error}")
    if reset_ipv6 and not ipv6:
        res = run_system(["netsh", "interface", "ipv6", "set", "dnsservers",
                          quoted, "source=dhcp"], timeout=15)
        (parts if res.ok else errors).append(
            f"{adapter} (ipv6): авто" if res.ok else f"{adapter} (ipv6): {res.human_error}")
    for family, servers in (("ipv4", ipv4 or []), ("ipv6", ipv6 or [])):
        if not servers:
            continue
        first = run_system(["netsh", "interface", family, "set", "dnsservers",
                            quoted, "static", servers[0], "primary"], timeout=15)
        if not first.ok:
            errors.append(f"{adapter} ({family}): {first.human_error}")
            continue
        parts.append(f"{adapter} ({family}): {servers[0]} (основной)")
        for index, ip in enumerate(servers[1:], start=2):
            extra = run_system(["netsh", "interface", family, "add", "dnsservers",
                                quoted, ip, f"index={index}"], timeout=15)
            if extra.ok:
                parts.append(f"{adapter} ({family}): {ip} (доп.)")
            else:
                errors.append(f"{adapter} ({family}) доп. {ip}: {extra.human_error}")
    return ShellResult(stdout="\n".join(parts), code=(0 if parts and not errors else -5),
                       via="netsh", human_error=("; ".join(errors[:3]) if errors else ""),
                       parts=parts)


def reset_dns_servers(adapter: str) -> ShellResult:
    """Возврат DNS адаптера на DHCP (авто) через netsh."""
    if not IS_WINDOWS:
        return ShellResult(code=-1, via="none", human_error=human_error("not_windows"))
    parts: list[str] = []
    errors: list[str] = []
    quoted = f'name="{adapter}"'
    for family in ("ipv4", "ipv6"):
        res = run_system(["netsh", "interface", family, "set", "dnsservers",
                          quoted, "source=dhcp"], timeout=15)
        if res.ok:
            parts.append(f"{adapter} ({family}): авто")
        else:
            errors.append(f"{adapter} ({family}): {res.human_error}")
    return ShellResult(stdout="\n".join(parts), code=(0 if parts else -5), via="netsh",
                       human_error=("; ".join(errors[:3]) if errors else ""), parts=parts)


def global_ipv6_count() -> tuple[int, ShellResult]:
    """Сколько в системе глобальных IPv6-адресов (признак «IPv6 работает»).

    Нужно проверке утечек: если IPv6 реально маршрутизируется, а IPv6-DNS не на
    UmbraNet — будет утечка мимо программы. Считаем через
    `netsh interface ipv6 show addresses`: адреса узнаваемы сами по себе, поэтому
    разбор не зависит от локализации. Служебные (link-local fe80, fec0, ::1,
    loopback) не считаем — они есть всегда и ни о чём не говорят.
    """
    res = run_system(["netsh", "interface", "ipv6", "show", "addresses"], timeout=15)
    if not res.ok:
        return 0, res
    count = 0
    for raw in res.stdout.splitlines():
        for ip in _IPV6_RE.findall(raw):
            # Зону срезаем: "fe80::1%12" и "fe80::1" — один и тот же адрес.
            low = ip.lower().split("%")[0]
            if low.startswith(("fe80", "fec0")) or low in ("::1", "::"):
                continue
            count += 1
    res.stdout = str(count)
    return count, res


def set_ipv6_enabled(adapter: str, enabled: bool) -> ShellResult:
    """Включает/выключает протокол IPv6 на адаптере через netsh (путь без PowerShell)."""
    state = "enabled" if enabled else "disabled"
    return run_system(["netsh", "interface", "ipv6", "set", "interface",
                       adapter, f"admin={state}"], timeout=15)


def kill_process(name: str = "", pid: int | None = None) -> ShellResult:
    """Закрывает процесс: `Stop-Process` → `taskkill`."""
    if pid:
        ps = run_ps(f"Stop-Process -Id {int(pid)} -Force -ErrorAction SilentlyContinue")
        if ps.ok:
            return ps
        native = run_system(["taskkill", "/F", "/PID", str(int(pid))], timeout=15)
    else:
        ps = run_ps(f"Stop-Process -Name '{name}' -Force -ErrorAction SilentlyContinue")
        if ps.ok:
            return ps
        native = run_system(["taskkill", "/F", "/IM", name], timeout=15)
    # taskkill со кодом 128 = «процесс не найден»: для зачистки это успех, а не
    # ошибка. Текст ошибки снимаем обязательно: иначе результат считается сбоем
    # (ShellResult.ok проверяет и код, и текст) и человек видит «не удалось»,
    # хотя процесс уже мёртв.
    if native.code == 128:
        native.code = 0
        native.human_error = ""
        native.stdout = native.stdout or "процесс уже закрыт"
    return _fallback(ps, native)


def reg_set(hive_path: str, name: str, value: str, reg_type: str = "REG_SZ") -> ShellResult:
    """Пишет значение в реестр: `Set-ItemProperty` → `reg add`."""
    ps_cmd = (f"$p = '{hive_path}'; if (-not (Test-Path $p)) {{ New-Item -Path $p -Force | Out-Null }}; "
              f"Set-ItemProperty -Path $p -Name '{name}' -Value '{value}' -Type String; "
              "Write-Output 'OK'")
    ps = run_ps(ps_cmd, timeout=15)
    if ps.ok and "OK" in ps.stdout:
        return ps
    native = run_system(["reg", "add", hive_path.replace(":", "").replace("/", "\\"),
                         "/v", name, "/t", reg_type, "/d", value, "/f"], timeout=15)
    return _fallback(ps, native)


def reg_delete(hive_path: str, name: str) -> ShellResult:
    """Удаляет значение из реестра: `Remove-ItemProperty` → `reg delete`."""
    ps = run_ps(f"Remove-ItemProperty -Path '{hive_path}' -Name '{name}' "
                "-ErrorAction SilentlyContinue; Write-Output 'OK'", timeout=10)
    if ps.ok and "OK" in ps.stdout:
        return ps
    native = run_system(["reg", "delete", hive_path.replace(":", "").replace("/", "\\"),
                         "/v", name, "/f"], timeout=15)
    if native.code in (1,) and "не удалось найти" in native.stderr.lower():
        # Значения и не было — цель достигнута. Текст ошибки снимаем: результат
        # считается сбоем, пока он есть (ShellResult.ok смотрит и код, и текст).
        native.code = 0
        native.human_error = ""
        native.stdout = native.stdout or "значения не было"
    return _fallback(ps, native)


# ── текст для человека: отчёт, интерфейс, логи ──────────────────────────────

def status_line() -> str:
    """Строка для отчёта диагностики: доступен ли PowerShell и чем это грозит."""
    status = powershell_status()
    if status["available"]:
        return f"PowerShell: доступен ({status.get('path') or 'powershell.exe'})"
    return "PowerShell: НЕДОСТУПЕН — " + warning_text().removeprefix("PowerShell недоступен — ")


def limitations() -> list[str]:
    """Что именно недоступно без PowerShell — человеческим языком, для интерфейса."""
    if powershell_available():
        return []
    return [
        f"{LIMIT_SHORT['log']} (вкладка «Логи» — только системные логи)",
        LIMIT_SHORT["repair"],
        LIMIT_SHORT["leak"],
    ]


def warning_text() -> str:
    """Полное предупреждение одной строкой: причина, чем обходимся, что недоступно,
    что сделать. Одно место на интерфейс и отчёт — тексты не разъезжаются.
    """
    status = powershell_status()
    if status["available"]:
        return ""
    reason = REASONS.get(status.get("reason", "error"), status.get("reason", ""))
    if status.get("reason") == "not_windows":
        return "PowerShell недоступен: это не Windows."
    text = (f"PowerShell недоступен — {reason}. Работаем через "
            f"{', '.join(fallback_names())}.")
    limits = [LIMIT_SHORT["log"], LIMIT_SHORT["repair"], LIMIT_SHORT["leak"]]
    text += " Недоступны: " + "; ".join(limits) + "."
    return f"{text} {POWERSHELL_HINT}"


def fallback_names() -> list[str]:
    """Какими утилитами программа обходится без PowerShell (для текста предупреждения)."""
    return ["netsh", "ipconfig", "taskkill", "reg"]
