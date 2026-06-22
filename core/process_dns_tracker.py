"""
UmbraNet — DnsProcessTracker
=============================

Сопоставляет DNS-запросы с процессами, которые их сделали, чтобы работала
маршрутизация «по приложению» (config["routed_processes"]).

ПОЧЕМУ ТАК, А НЕ «PID ПРЯМО В RESOLVE»
--------------------------------------
В Windows приложение не шлёт DNS-пакет напрямую: dnsapi.dll передаёт запрос
службе DNS-Client через внутренний IPC, и до нашего локального резолвера
(127.0.0.1:53) запрос доходит уже ОТ СЛУЖБЫ, без PID приложения. Получить PID
«в момент resolve» нельзя без инъекции в dnsapi.dll (так делает малварь — нам
нельзя).

Штатный путь: Windows сам пишет событие 3008 в журнал
`Microsoft-Windows-DNS-Client/Operational`, и в нём есть И доменное имя
(QueryName), И PID процесса (Execution ProcessID). Мы фоном читаем этот журнал,
резолвим PID → имя .exe и поддерживаем таблицу `домен → {процессы, время}`.

ОГРАНИЧЕНИЯ (честно)
--------------------
- Корреляция «догоняющая»: событие приходит с задержкой, поэтому ПЕРВЫЙ запрос
  нового домена может проскочить по системному DNS. Со второго запроса (а их при
  загрузке страницы десятки) — стабильно. Поэтому таблица «липкая» (sticky TTL).
- Только Windows. На других ОС / без журнала трекер тихо отключается и
  domain_requested_by() всегда возвращает False → поведение = маршрутизация
  только по доменам (как было раньше). Ничего не ломается.
"""

import logging
import sys
import threading
import time

log = logging.getLogger("UmbraNet.DnsProcessTracker")

IS_WINDOWS = sys.platform == "win32"

DNS_CLIENT_LOG = "Microsoft-Windows-DNS-Client/Operational"

# Сколько секунд помним связку домен→процесс. «Липкость» нужна, чтобы повторные
# запросы того же домена надёжно маршрутизировались, даже если первый проскочил.
DEFAULT_STICKY_TTL = 300  # 5 минут

# Как часто опрашиваем журнал (сек). Чаще = быстрее реакция, но больше нагрузка.
DEFAULT_POLL_INTERVAL = 3.0

# Сколько событий читаем за один опрос (страховка от лавины событий).
MAX_EVENTS_PER_POLL = 400


def _normalize_domain(domain: str) -> str:
    return str(domain or "").rstrip(".").strip().lower()


def _normalize_proc(name: str) -> str:
    return str(name or "").strip().lower()


class DnsProcessTracker:
    """Поддерживает актуальную таблицу `домен → {exe_name: last_seen_ts}`.

    Потокобезопасен. Если запуск невозможен (не Windows / нет журнала / нет
    прав), остаётся в «пустом» состоянии: domain_requested_by() → False.
    """

    def __init__(self, sticky_ttl: int = DEFAULT_STICKY_TTL,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 ps_runner=None, pid_resolver=None):
        self.sticky_ttl = max(1, int(sticky_ttl))
        self.poll_interval = max(0.2, float(poll_interval))

        # Внедрение зависимостей для тестируемости (без реального Windows):
        #   ps_runner(command) -> (stdout, stderr, code)
        #   pid_resolver(pid)  -> exe_name (str) | None
        self._ps_runner = ps_runner
        self._pid_resolver = pid_resolver

        # domain -> { exe_name(lower): last_seen_monotonic }
        self._table = {}
        self._lock = threading.Lock()

        self._running = False
        self._thread = None
        self._available = False           # удалось ли реально запустить чтение
        self._last_error = ""
        self._last_record_id = 0          # для инкрементального чтения журнала
        self._pid_cache = {}              # pid -> (exe_name, ts) маленький кэш

    # ── Публичный статус (для UI) ────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self._available

    @property
    def running(self) -> bool:
        return self._running

    def status_text(self) -> str:
        if not IS_WINDOWS:
            return "Маршрутизация по процессам доступна только в Windows"
        if not self._running:
            return "Трекер процессов остановлен"
        if self._available:
            return "Трекер процессов активен (журнал DNS-Client читается)"
        if self._last_error:
            return f"Трекер недоступен: {self._last_error}"
        return "Трекер запускается…"

    def stats(self) -> dict:
        with self._lock:
            return {
                "domains_tracked": len(self._table),
                "available": self._available,
                "running": self._running,
                "last_error": self._last_error,
            }

    # ── Основной запрос из resolve()/is_domain_routed ────────────────────────
    def domain_requested_by(self, domain: str, process_names) -> bool:
        """True, если `domain` (или его родительский домен) недавно запрашивал
        какой-либо процесс из `process_names`.

        process_names — итерируемое имён .exe (регистр не важен).
        """
        if not process_names:
            return False
        wanted = {_normalize_proc(p) for p in process_names if _normalize_proc(p)}
        if not wanted:
            return False

        domain = _normalize_domain(domain)
        if not domain:
            return False

        now = time.monotonic()
        deadline = now - self.sticky_ttl

        # Проверяем сам домен и все его родительские суффиксы:
        # ab.chatgpt.com → ab.chatgpt.com, chatgpt.com, com
        candidates = []
        parts = domain.split(".")
        for i in range(len(parts) - 1):
            candidates.append(".".join(parts[i:]))
        candidates.append(domain)  # на случай одноуровневого имени

        with self._lock:
            for cand in candidates:
                procs = self._table.get(cand)
                if not procs:
                    continue
                for exe, ts in procs.items():
                    if ts >= deadline and exe in wanted:
                        return True
        return False

    # ── Жизненный цикл ───────────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="DnsProcessTracker"
        )
        self._thread.start()
        log.info("DnsProcessTracker запущен")

    def stop(self):
        self._running = False
        log.info("DnsProcessTracker остановлен")

    # ── Внутреннее ───────────────────────────────────────────────────────────
    def _get_ps_runner(self):
        if self._ps_runner is not None:
            return self._ps_runner
        # Ленивая привязка к реальному PowerShell-раннеру (с cp866-фиксом).
        from process_monitor import _run_ps
        return _run_ps

    def _get_pid_resolver(self):
        if self._pid_resolver is not None:
            return self._pid_resolver
        return self._resolve_pid_psutil

    @staticmethod
    def _resolve_pid_psutil(pid):
        try:
            import psutil
            return psutil.Process(int(pid)).name()
        except Exception:
            return None

    def _ensure_log_enabled(self) -> bool:
        """Включает журнал DNS-Client/Operational (он по умолчанию выключен)."""
        run = self._get_ps_runner()
        cmd = (
            f"$l = Get-WinEvent -ListLog '{DNS_CLIENT_LOG}'; "
            "if (-not $l.IsEnabled) { "
            "  $l.IsEnabled = $true; $l.SaveChanges(); Write-Output 'ENABLED' "
            "} else { Write-Output 'ALREADY' }"
        )
        stdout, stderr, code = run(cmd, timeout=15)
        if code == 0 and ("ENABLED" in stdout or "ALREADY" in stdout):
            return True
        self._last_error = stderr or "не удалось включить журнал DNS-Client"
        return False

    def _build_fetch_command(self) -> str:
        """PowerShell: читает новые события 3008 (домен + PID) в виде PID|домен."""
        # FilterXPath: только 3008 (запрос завершён) с EventRecordID больше уже
        # прочитанного — так читаем инкрементально, не перечитывая старое.
        xpath = (
            f"Event[System[EventID=3008 and EventRecordID > {self._last_record_id}]]"
        )
        return (
            "$ev = Get-WinEvent -LogName '" + DNS_CLIENT_LOG + "' "
            "-FilterXPath \"" + xpath + "\" "
            "-MaxEvents " + str(MAX_EVENTS_PER_POLL) + " "
            "-ErrorAction SilentlyContinue; "
            "foreach ($e in $ev) { "
            "  $x = [xml]$e.ToXml(); "
            "  $pid2 = $x.Event.System.Execution.ProcessID; "
            "  $name = ($x.Event.EventData.Data | "
            "           Where-Object {$_.Name -eq 'QueryName'}).'#text'; "
            "  $rid = $e.RecordId; "
            "  Write-Output ($rid.ToString() + '|' + $pid2 + '|' + $name) "
            "}"
        )

    def _poll_once(self):
        run = self._get_ps_runner()
        resolve_pid = self._get_pid_resolver()
        stdout, stderr, code = run(self._build_fetch_command(), timeout=20)
        if code != 0:
            # Журнал может временно отдавать ошибку — не считаем фатальной.
            if stderr:
                self._last_error = stderr
            return

        max_rid = self._last_record_id
        for line in stdout.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            try:
                rid_str, pid_str, name = line.split("|", 2)
                rid = int(rid_str)
            except ValueError:
                continue
            if rid > max_rid:
                max_rid = rid

            domain = _normalize_domain(name)
            if not domain:
                continue
            exe = self._resolve_exe(pid_str, resolve_pid)
            if not exe:
                continue
            self._record(domain, exe)

        if max_rid > self._last_record_id:
            self._last_record_id = max_rid

    def _resolve_exe(self, pid_str, resolve_pid):
        try:
            pid = int(pid_str)
        except (TypeError, ValueError):
            return None
        now = time.monotonic()
        cached = self._pid_cache.get(pid)
        if cached and (now - cached[1]) < 30:
            return cached[0]
        name = resolve_pid(pid)
        name = _normalize_proc(name) if name else None
        if name:
            self._pid_cache[pid] = (name, now)
        return name

    def _record(self, domain: str, exe: str):
        now = time.monotonic()
        with self._lock:
            procs = self._table.get(domain)
            if procs is None:
                procs = {}
                self._table[domain] = procs
            procs[exe] = now

    def _prune(self):
        now = time.monotonic()
        deadline = now - self.sticky_ttl
        with self._lock:
            dead_domains = []
            for domain, procs in self._table.items():
                stale = [e for e, ts in procs.items() if ts < deadline]
                for e in stale:
                    procs.pop(e, None)
                if not procs:
                    dead_domains.append(domain)
            for d in dead_domains:
                self._table.pop(d, None)
        # чистим pid-кэш
        with self._lock:
            self._pid_cache = {
                p: v for p, v in self._pid_cache.items()
                if (now - v[1]) < 60
            }

    def _loop(self):
        if not IS_WINDOWS:
            self._available = False
            self._last_error = "не Windows"
            # Поток сразу завершается — трекер «пустой», ничего не ломает.
            return

        if not self._ensure_log_enabled():
            self._available = False
            log.warning("DnsProcessTracker: %s", self._last_error)
            # Останемся в цикле и будем повторять попытку включить журнал,
            # т.к. права/состояние могут появиться позже.
        else:
            self._available = True
            log.info("DnsProcessTracker: журнал DNS-Client включён, читаем события")

        last_prune = time.monotonic()
        while self._running:
            try:
                if not self._available:
                    if self._ensure_log_enabled():
                        self._available = True
                        log.info("DnsProcessTracker: журнал включён")
                else:
                    self._poll_once()

                if time.monotonic() - last_prune > 30:
                    self._prune()
                    last_prune = time.monotonic()
            except Exception as exc:
                self._last_error = str(exc)
                log.debug("DnsProcessTracker poll error: %s", exc)
            time.sleep(self.poll_interval)
