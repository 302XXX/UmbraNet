"""
UmbraNet - WinWS Engine

Обёртка над bin/winws.exe:
  • ищет bin/ рядом с программой;
  • запускает winws.exe с выбранной стратегией;
  • пишет stdout/stderr WinWS в отдельный лог, чтобы не было «молча упал»;
  • хранит last_error/last_args для диагностики UI.
"""
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("UmbraNet.WinWSEngine")


class WinWSEngine:
    def __init__(self):
        self.process = None
        self._log_handle = None
        self._bin_dir = self._find_bin_dir()
        self._exe_path = self._bin_dir / "winws.exe"
        self._log_path = self._bin_dir.parent / "winws.log"
        self.last_error = ""
        self.last_args = []
        self.last_cmd = []
        self.last_exit_code = None

    def _find_bin_dir(self):
        current = Path(__file__).resolve().parent
        for _ in range(6):
            candidate = current / "bin"
            if candidate.exists():
                return candidate
            current = current.parent
        return Path.cwd() / "bin"

    @property
    def exe_path(self):
        return self._exe_path

    @property
    def log_path(self):
        return self._log_path

    def is_available(self):
        return self._exe_path.exists()

    def is_running(self):
        if self.process is None:
            return False
        return self.process.poll() is None

    def _tail_log(self, max_chars: int = 2000) -> str:
        try:
            if not self._log_path.exists():
                return ""
            data = self._log_path.read_text(encoding="utf-8", errors="replace")
            return data[-max_chars:].strip()
        except Exception:
            return ""

    def _close_log_handle(self):
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def start(self, args):
        self.last_error = ""
        self.last_exit_code = None
        self.last_args = list(args or [])
        self.last_cmd = []

        if not self.is_available():
            self.last_error = f"winws.exe не найден: {self._exe_path}"
            log.error(self.last_error)
            return False
        if not args:
            self.last_error = "Пустые аргументы WinWS: стратегия не найдена или повреждена"
            log.error(self.last_error)
            return False
        if self.is_running():
            self.stop()

        cmd = [str(self._exe_path)] + list(args)
        self.last_cmd = cmd
        try:
            log.info("Запуск WinWS: %s", " ".join(cmd))
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._close_log_handle()
            self._log_handle = open(self._log_path, "a", encoding="utf-8", errors="replace")
            self._log_handle.write("\n" + "=" * 80 + "\n")
            self._log_handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " WinWS start\n")
            self._log_handle.write("CMD: " + " ".join(cmd) + "\n")
            self._log_handle.flush()

            env = os.environ.copy()
            # Чтобы WinWS точно видел WinDivert.dll/cygwin1.dll рядом с собой.
            env["PATH"] = str(self._bin_dir) + os.pathsep + env.get("PATH", "")

            self.process = subprocess.Popen(
                cmd,
                cwd=str(self._bin_dir),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            time.sleep(0.8)
            if self.process.poll() is not None:
                self.last_exit_code = self.process.returncode
                tail = self._tail_log()
                self.last_error = f"WinWS упал при старте (код {self.last_exit_code})"
                if tail:
                    self.last_error += f": {tail[-500:]}"
                log.error(self.last_error)
                self.process = None
                self._close_log_handle()
                return False
            log.info("WinWS запущен (PID %s), лог: %s", self.process.pid, self._log_path)
            return True
        except Exception as exc:
            self.last_error = f"Ошибка запуска WinWS: {exc}"
            log.error(self.last_error)
            self.process = None
            self._close_log_handle()
            return False


    def _kill_orphan_processes(self) -> bool:
        """Добивает winws.exe из нашей папки bin, если Python потерял Popen-объект.

        Это главный DPI-safety-net: при AI-подборе/краше/гонке Qt потоков
        winws.exe может остаться без self.process и продолжить держать WinDivert
        и файлы UmbraNet/bin. Убиваем только экземпляры, связанные с текущей
        папкой bin, чтобы не трогать чужие установки zapret/winws.
        """
        if os.name != "nt":
            return False
        try:
            exe = str(self._exe_path)
            bin_dir = str(self._bin_dir)
            # PowerShell-переменные передаём через single-quoted literals;
            # одинарную кавычку экранируем удвоением.
            exe_ps = exe.replace("'", "''")
            bin_ps = bin_dir.rstrip("\\/").replace("'", "''")
            ps = (
                f"$exe='{exe_ps}'; $bin='{bin_ps}'; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -ieq 'winws.exe' -and ("
                " ($_.ExecutablePath -and $_.ExecutablePath -ieq $exe) -or "
                " ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($bin, [System.StringComparison]::OrdinalIgnoreCase)) -or "
                " ($_.CommandLine -and $_.CommandLine.Contains($bin))"
                ") } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
            return res.returncode == 0
        except Exception as exc:
            self.last_error = f"Ошибка orphan-cleanup WinWS: {exc}"
            log.debug(self.last_error)
            return False

    def cleanup_orphans(self, stop_driver: bool = False) -> bool:
        """Жёсткая очистка DPI-хвостов текущей установки UmbraNet."""
        killed = self._kill_orphan_processes()
        if stop_driver and os.name == "nt":
            try:
                ps = (
                    "foreach ($s in 'WinDivert','WinDivert14','WinDivert64') { "
                    "  Stop-Service -Name $s -Force -ErrorAction SilentlyContinue "
                    "}"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=5,
                )
            except Exception:
                pass
        return killed

    def stop(self):
        if not self.process:
            self._close_log_handle()
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.last_exit_code = self.process.returncode
            log.info("WinWS остановлен (код %s)", self.last_exit_code)
        except Exception as exc:
            self.last_error = f"Ошибка остановки WinWS: {exc}"
            log.error(self.last_error)
        finally:
            self.process = None
            # На Windows terminate/kill иногда оставляет winws.exe без Popen-ссылки
            # (особенно при cygwin/winws + Qt thread races). Добиваем только наш exe.
            self._kill_orphan_processes()
            self._close_log_handle()

    def restart(self, args):
        """Перезапускает winws.exe с новыми аргументами."""
        log.info("Перезапуск WinWS...")
        self.stop()
        time.sleep(0.2)
        return self.start(args)

    def status(self) -> dict:
        return {
            "available": self.is_available(),
            "running": self.is_running(),
            "exe_path": str(self._exe_path),
            "log_path": str(self._log_path),
            "last_error": self.last_error,
            "last_exit_code": self.last_exit_code,
            "last_args": list(self.last_args),
            "last_cmd": list(self.last_cmd),
        }


_engine = None


def get_winws_engine():
    global _engine
    if _engine is None:
        _engine = WinWSEngine()
    return _engine
