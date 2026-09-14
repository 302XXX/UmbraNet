"""
UmbraNet — QUIC probe
=====================

Безопасная проверка доступности QUIC/HTTP3 поверх UDP/443.

Это НЕ обход и не DPI-desync. Мы просто пытаемся выполнить QUIC handshake через
`aioquic`. Если aioquic не установлен — возвращаем неопределённый результат,
чтобы не давать ложных выводов.
"""

from __future__ import annotations

import asyncio
import ssl


def quic_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("aioquic") is not None
    except Exception:
        return False


async def _probe_quic_async(host: str, port: int, timeout: float) -> tuple[bool | None, str]:
    from aioquic.asyncio.client import connect
    from aioquic.quic.configuration import QuicConfiguration

    config = QuicConfiguration(
        is_client=True,
        alpn_protocols=["h3", "h3-29", "h3-32"],
    )
    # Для диагностики доступности QUIC нам важен сам UDP/QUIC path, а не PKI.
    # Hostname всё равно передаём как SNI/server_name.
    config.verify_mode = ssl.CERT_NONE
    config.server_name = host

    async def _do():
        async with connect(
            host,
            port,
            configuration=config,
            wait_connected=True,
        ):
            return True, "QUIC handshake OK"

    return await asyncio.wait_for(_do(), timeout=timeout)


def probe_quic(host: str, port: int = 443, timeout: float = 4.0) -> tuple[bool | None, str]:
    """Проверяет QUIC handshake.

    Возвращает:
      True  — QUIC handshake прошёл;
      False — QUIC явно не прошёл/UDP path недоступен/сервер отказал;
      None  — проверить невозможно локально (например, нет aioquic).
    """
    host = (host or "").strip()
    if not host:
        return None, "host не задан"
    if not quic_available():
        return None, "QUIC-probe недоступен: нужен aioquic"

    try:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(_probe_quic_async(host, port, timeout))
        finally:
            try:
                loop.close()
            except Exception:
                pass
    except asyncio.TimeoutError:
        return False, f"таймаут QUIC/UDP:{port}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
