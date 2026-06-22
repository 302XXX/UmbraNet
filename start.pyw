"""
UmbraNet — точка запуска (без консоли).

Запускается через pythonw.exe — окно консоли не появляется.
При любой ошибке показывает MessageBox вместо молчаливого падения.

Почему нужен этот файл (а не просто python -m umbranet.main):
  При запуске через UAC (Request Administrator) Windows устанавливает
  рабочую директорию в System32, а не в папку программы. Команда
  "python -m umbranet.main" тогда не находит пакет umbranet.
  Этот файл добавляет папку программы в sys.path по АБСОЛЮТНОМУ пути
  через __file__, что работает независимо от рабочей директории.
"""

import sys
import os

# Абсолютный путь к корню репо (папка где лежит этот файл)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _write_error_log(title: str, msg: str) -> None:
    """Пишет ошибку рядом с программой — полезно, если окно сразу закрывается."""
    try:
        log_path = os.path.join(BASE_DIR, "umbranet_error.log")
        with open(log_path, "a", encoding="utf-8") as f:
            import datetime
            f.write(f"\n[{datetime.datetime.now()}] {title}\n{msg}\n")
    except Exception:
        pass


def _show_error(title: str, msg: str) -> None:
    """Показывает ошибку через MessageBox и всегда дублирует её в файл."""
    _write_error_log(title, msg)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, msg)
        root.destroy()
    except Exception:
        pass


def _check_dependencies() -> list[str]:
    """Проверяет наличие обязательных зависимостей."""
    missing = []
    required = {
        "PySide6": "PySide6",
        "dnslib":  "dnslib",
        "requests": "requests",
        "psutil":  "psutil",
        # Эти два пакета нужны, чтобы в интерфейсе не были заблокированы
        # транспорты DoQ и DNSCrypt после обычной установки через install.bat.
        "aioquic": "aioquic",
        "nacl": "pynacl",
    }
    for import_name, pkg_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg_name)
    return missing


def main():
    # 0. Автоматическое повышение прав (UAC) на Windows
    import sys
    import os
    import ctypes

    if sys.platform == "win32":
        try:
            is_elevated = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            is_elevated = False

        if not is_elevated:
            try:
                script = os.path.abspath(__file__)
                params = f'"{script}"'
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, params, None, 1
                )
                sys.exit(0)
            except Exception as exc:
                _show_error(
                    "Ошибка повышения прав UAC",
                    f"Не удалось запросить права администратора:\n{exc}\n\n"
                    f"Пожалуйста, запустите программу через start.bat от имени администратора."
                )
                return

    # 1. Проверяем обязательные зависимости
    missing = _check_dependencies()
    if missing:
        _show_error(
            "UmbraNet — отсутствуют зависимости",
            f"Не установлены пакеты:\n{', '.join(missing)}\n\n"
            f"Запустите install.bat для установки зависимостей.\n\n"
            f"Или выполните в терминале из папки UmbraNet:\n"
            f".venv\\Scripts\\python.exe -m pip install {' '.join(missing)}"
        )
        return

    # 2. Запускаем основное приложение
    try:
        from umbranet.app import run
        run()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        _show_error(
            "UmbraNet — ошибка запуска",
            f"Произошла ошибка:\n\n{e}\n\n{err[-1000:]}"
        )


if __name__ == "__main__":
    main()
