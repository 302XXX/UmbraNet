"""
Живой тест pipe-протокола watchdog (P0-3).

Отличие от tests/test_dns_restore.py: там проверяется логика на io.BytesIO,
здесь запускаются НАСТОЯЩИЕ процессы и настоящие pipe. Именно так ловится
всё, что связано с реальным EOF, с закрытием канала и со смертью родителя.

Зачем это отдельно: старый watchdog опрашивал `tasklist` и на переиспользовании
PID считал мёртвого родителя живым — интернет у пользователя не восстанавливался.
Проверить такое «в лоб» можно только реальной смертью процесса.

Используется режим `--check`: watchdog ждёт сигнал и печатает, что сделал бы,
НЕ трогая реальный DNS. Поэтому тест безопасен и не требует прав администратора.

Запуск: python -m pytest tests/test_watchdog_real_pipe.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHDOG = ROOT / "core" / "watchdog.py"

pytestmark = pytest.mark.skipif(
    not WATCHDOG.exists(), reason="core/watchdog.py не найден"
)


def _run_parent(parent_body: str, wait_after: float = 2.5) -> str:
    """Запускает «родителя», который стартует watchdog и выполняет parent_body.

    Вывод watchdog пишется в файл, а не в pipe: в сценариях с крашем родителя
    читать pipe попросту некому, и результат потерялся бы.
    """
    out_path = tempfile.mktemp(suffix=".watchdog.txt")
    code = f"""
import subprocess, sys, os, time
out = open({out_path!r}, "wb")
proc = subprocess.Popen([sys.executable, {str(WATCHDOG)!r}, "--check"],
                        stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT)
{parent_body}
"""
    try:
        subprocess.run([sys.executable, "-c", code],
                       capture_output=True, timeout=40, check=False)
        time.sleep(wait_after)          # даём watchdog проснуться на EOF
        if os.path.exists(out_path):
            return pathlib.Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        return ""
    finally:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass


def test_real_pipe_clean_exit_skips():
    """HELLO + CLEAN по настоящему pipe → watchdog ничего не делает."""
    out = _run_parent("""
proc.stdin.write(b"HELLO\\n"); proc.stdin.flush()
time.sleep(0.3)
proc.stdin.write(b"CLEAN\\n"); proc.stdin.flush(); proc.stdin.close()
""")
    assert "signal='CLEAN'" in out, f"ожидался CLEAN, получено: {out!r}"
    assert "action=skip" in out, "при штатном выходе сеть трогать нельзя"


def test_real_pipe_restore_request():
    """HELLO + RESTORE → watchdog возвращает DNS."""
    out = _run_parent("""
proc.stdin.write(b"HELLO\\n"); proc.stdin.flush()
time.sleep(0.3)
proc.stdin.write(b"RESTORE\\n"); proc.stdin.flush(); proc.stdin.close()
""")
    assert "signal='RESTORE'" in out and "action=restore" in out, f"получено: {out!r}"


def test_real_pipe_parent_crash_is_detected():
    """Родитель упал после HELLO → пустой сигнал и возврат DNS.

    Это главный сценарий, который старый код с tasklist мог пропустить.
    """
    out = _run_parent("""
proc.stdin.write(b"HELLO\\n"); proc.stdin.flush()
time.sleep(0.3)
os._exit(1)          # краш без прощания
""")
    assert "signal=''" in out, f"смерть родителя должна давать пустой сигнал: {out!r}"
    assert "action=restore" in out, "DNS обязан вернуться"


def test_real_pipe_parent_crash_before_handshake():
    """Родитель умер, не успев поздороваться → всё равно возвращаем DNS."""
    out = _run_parent("os._exit(1)")
    assert "action=restore" in out, f"получено: {out!r}"


def test_real_pipe_sigkill_is_detected():
    """«Снять задачу» в диспетчере (SIGKILL/kill) → DNS возвращается.

    Самый жёсткий случай: родитель не выполнил ни строчки cleanup-кода.
    """
    out_path = tempfile.mktemp(suffix=".watchdog.txt")
    code = f"""
import subprocess, sys, os, time
out = open({out_path!r}, "wb")
proc = subprocess.Popen([sys.executable, {str(WATCHDOG)!r}, "--check"],
                        stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT)
proc.stdin.write(b"HELLO\\n"); proc.stdin.flush()
time.sleep(30)
"""
    try:
        parent = subprocess.Popen([sys.executable, "-c", code])
        time.sleep(1.5)
        parent.kill()
        parent.wait(timeout=10)
        time.sleep(2.5)
        out = pathlib.Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        assert "action=restore" in out, f"после kill DNS должен вернуться: {out!r}"
    finally:
        try:
            os.path.exists(out_path) and os.remove(out_path)
        except OSError:
            pass
