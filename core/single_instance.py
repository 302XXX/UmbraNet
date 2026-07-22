"""
UmbraNet — Single Instance Guard
=================================

Не даёт запустить вторую копию программы.

На Windows используем ИМЕНОВАННЫЙ MUTEX (CreateMutex) — он:
  • глобален для всей системы и виден из ЛЮБОГО процесса, включая запущенный
    под UAC с правами администратора (важно: lock-файл в %TEMP% этого не давал,
    т.к. у админ-процесса другой TEMP);
  • автоматически освобождается ОС при завершении процесса (даже при падении).

На не-Windows — fallback на эксклюзивную блокировку lock-файла (fcntl).

Использование:
    guard = SingleInstance()
    if guard.already_running():
        ...   # вторая копия — выйти / показать сообщение
"""

import logging
import os
import sys
import tempfile

log = logging.getLogger("UmbraNet.SingleInstance")

IS_WINDOWS = sys.platform == "win32"

# Имя должно быть уникальным и стабильным. Префикс Global\\ делает mutex видимым
# во всех сессиях (в т.ч. между обычным и elevated процессом).
MUTEX_NAME = "Global\\UmbraNet_SingleInstance_Mutex_v1"
LOCK_FILE_NAME = "umbranet.lock"

ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = None):
        self._mutex = None
        self._fh = None
        self._locked = False
        self._mutex_name = name or MUTEX_NAME
        self._lock_path = os.path.join(
            tempfile.gettempdir(), name or LOCK_FILE_NAME
        )
        if IS_WINDOWS:
            self._acquire_mutex()
        else:
            self._acquire_file()

    # ── Windows: именованный mutex ───────────────────────────────────────────
    def _acquire_mutex(self):
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

            # bInitialOwner=False — нам важен сам факт существования mutex
            self._mutex = kernel32.CreateMutexW(None, False, self._mutex_name)
            last_error = kernel32.GetLastError()

            if not self._mutex:
                # не смогли создать mutex — на всякий случай не блокируем запуск
                log.debug("CreateMutexW вернул NULL, err=%s", last_error)
                self._locked = True
                return

            if last_error == ERROR_ALREADY_EXISTS:
                # mutex уже существует → запущена другая копия
                self._locked = False
            else:
                self._locked = True
        except Exception as exc:
            log.debug("Mutex недоступен (%s), fallback на файл", exc)
            self._acquire_file()

    # ── Не-Windows: файловая блокировка ──────────────────────────────────────
    def _acquire_file(self):
        try:
            self._fh = open(self._lock_path, "a+")
            if IS_WINDOWS:
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(str(os.getpid()))
                self._fh.flush()
            except Exception:
                pass
            self._locked = True
        except OSError:
            self._locked = False
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        except Exception as exc:
            log.debug("Файловая блокировка недоступна: %s", exc)
            self._locked = True

    # ── Публичное API ────────────────────────────────────────────────────────
    def already_running(self) -> bool:
        """True, если другая копия уже работает."""
        return not self._locked

    @property
    def lock_path(self) -> str:
        return self._lock_path

    def release(self):
        # mutex
        if self._mutex:
            try:
                import ctypes
                ctypes.windll.kernel32.ReleaseMutex(self._mutex)
                ctypes.windll.kernel32.CloseHandle(self._mutex)
            except Exception:
                pass
            self._mutex = None
        # файл
        if self._fh:
            try:
                if not IS_WINDOWS:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            try:
                os.remove(self._lock_path)
            except Exception:
                pass
        self._locked = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


# ── Сигнал «покажи существующее окно» ────────────────────────────────────────
# Вторая копия не запускается, но создаёт файл-флаг; уже работающая копия
# периодически его проверяет и поднимает своё окно. Так повторный клик по
# start.bat не плодит копии, а показывает уже открытую программу.

_SHOW_SIGNAL_FILE = os.path.join(tempfile.gettempdir(), "umbranet.show")


def request_show_existing():
    """Вызывается ВТОРОЙ копией: просит работающую копию показать окно."""
    try:
        with open(_SHOW_SIGNAL_FILE, "w") as f:
            f.write("show")
    except Exception:
        pass


def consume_show_request() -> bool:
    """Вызывается работающей копией: True, если поступил запрос показать окно."""
    try:
        if os.path.exists(_SHOW_SIGNAL_FILE):
            os.remove(_SHOW_SIGNAL_FILE)
            return True
    except Exception:
        pass
    return False
