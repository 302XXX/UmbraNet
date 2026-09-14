"""UmbraNet - точка входа.

Запуск (для разработки):  python -m umbranet.main
"""

from __future__ import annotations


def main() -> None:
    from umbranet.app import run
    run()


if __name__ == "__main__":
    main()
