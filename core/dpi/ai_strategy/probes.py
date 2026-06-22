"""
Basic probes for future UmbraNet AI strategy autotuning.

Проба — это проверка, работает ли тестовая цель на текущей сети/текущей
DPI-стратегии. Этот модуль пока НЕ переключает стратегии и НЕ создаёт Uz.
Он только даёт базовые измерения для будущего автоподбора.

Текущий уровень:
  • YouTube media_readiness: generate_204 + player page markers + API/static/media endpoints;
  • Discord voice_readiness: API/CDN/media + WebSocket gateway + voice regions.

Полный браузерный playback без headless-браузера не выполняется, но probes
проверяют именно те части, которые чаще ломаются при DPI-блокировке.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
from typing import Any


DEFAULT_TIMEOUT = 6.0


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _result(name: str, ok: bool, **extra) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), **extra}


def resolve_probe(host: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    started = _now_ms()
    try:
        # socket.getaddrinfo не принимает timeout напрямую, но общий timeout
        # процесса не меняем; это быстрый системный DNS-запрос.
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addrs: list[str] = []
        seen: set[str] = set()
        for info in infos:
            addr = str(info[4][0])
            if addr not in seen:
                seen.add(addr)
                addrs.append(addr)
        return _result(
            "dns",
            bool(addrs),
            host=host,
            addresses=addrs[:8],
            ms=round(_now_ms() - started, 1),
        )
    except Exception as exc:
        return _result("dns", False, host=host, error=str(exc), ms=round(_now_ms() - started, 1))


def https_probe(host: str, path: str = "/", method: str = "HEAD",
                timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Минимальная DNS/TCP/TLS/HTTP проверка без внешних зависимостей."""
    started = _now_ms()
    method = (method or "HEAD").upper()
    dns = resolve_probe(host, timeout=timeout)
    if not dns.get("ok"):
        return _result(
            "https",
            False,
            host=host,
            path=path,
            stage="dns",
            dns=dns,
            ms=round(_now_ms() - started, 1),
        )

    sock = None
    ssock = None
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        sock = raw
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(raw, server_hostname=host)
        request = (
            f"{method} {path or '/'} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: UmbraNet-Probe/1.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        ssock.sendall(request)
        data = ssock.recv(4096)
        first_line = data.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace") if data else ""
        status = 0
        parts = first_line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
        # Для probes важен факт HTTP-ответа. 403/404 тоже доказывают, что TLS/HTTP
        # дошли до сервера; 5xx считаем слабым, но сетевой путь всё равно есть.
        ok = 100 <= status < 500
        return _result(
            "https",
            ok,
            host=host,
            path=path,
            method=method,
            status=status,
            first_line=first_line,
            stage="http" if ok else "http_status",
            dns=dns,
            ms=round(_now_ms() - started, 1),
        )
    except Exception as exc:
        return _result(
            "https",
            False,
            host=host,
            path=path,
            method=method,
            stage="tls_or_http",
            dns=dns,
            error=str(exc),
            ms=round(_now_ms() - started, 1),
        )
    finally:
        for s in (ssock, sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def https_content_probe(host: str, path: str = "/", contains_any: list[str] | None = None,
                        timeout: float = DEFAULT_TIMEOUT, max_bytes: int = 65536) -> dict[str, Any]:
    """GET-проверка с чтением тела и поиском маркеров.

    Нужна для YouTube: обычный HEAD/204 может проходить, но страница плеера
    может быть подменена/обрезана. Маркеры ytInitialPlayerResponse/googlevideo
    доказывают, что клиент дошёл до настоящей страницы плеера, а не только до
    заглушки/редиректа.
    """
    started = _now_ms()
    markers = [str(x) for x in (contains_any or []) if str(x)]
    dns = resolve_probe(host, timeout=timeout)
    if not dns.get("ok"):
        return _result("https_content", False, host=host, path=path, stage="dns", dns=dns,
                       ms=round(_now_ms() - started, 1))

    sock = None
    ssock = None
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        sock = raw
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(raw, server_hostname=host)
        request = (
            f"GET {path or '/'} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) UmbraNet-Probe/1.0\r\n"
            "Accept: text/html,application/json,*/*\r\n"
            "Accept-Language: en-US,en;q=0.9\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        ssock.sendall(request)
        chunks: list[bytes] = []
        total = 0
        ssock.settimeout(timeout)
        while total < max_bytes:
            chunk = ssock.recv(min(8192, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        head = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace") if data else ""
        first_line = head.split("\r\n", 1)[0] if head else ""
        status = 0
        parts = first_line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
        text = data.decode("utf-8", "ignore")
        found = [m for m in markers if m in text]
        http_ok = 100 <= status < 500
        ok = http_ok and (bool(found) if markers else True)
        return _result(
            "https_content",
            ok,
            host=host,
            path=path,
            method="GET",
            status=status,
            first_line=first_line,
            stage="content" if ok else ("content_marker" if http_ok else "http_status"),
            markers=markers,
            found_markers=found,
            bytes=len(data),
            dns=dns,
            ms=round(_now_ms() - started, 1),
        )
    except Exception as exc:
        return _result(
            "https_content",
            False,
            host=host,
            path=path,
            method="GET",
            stage="tls_or_http",
            dns=dns,
            error=str(exc),
            ms=round(_now_ms() - started, 1),
        )
    finally:
        for sck in (ssock, sock):
            try:
                if sck:
                    sck.close()
            except Exception:
                pass


def _read_ws_frame(sock: ssl.SSLSocket, timeout: float) -> tuple[int, bytes]:
    sock.settimeout(timeout)
    header = sock.recv(2)
    if len(header) < 2:
        raise RuntimeError("empty websocket frame")
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    length = b2 & 0x7F
    if length == 126:
        ext = sock.recv(2)
        if len(ext) < 2:
            raise RuntimeError("short websocket frame length")
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = sock.recv(8)
        if len(ext) < 8:
            raise RuntimeError("short websocket frame length")
        length = struct.unpack("!Q", ext)[0]
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    return opcode, payload


def websocket_hello_probe(host: str, path: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """WebSocket handshake + чтение первого server frame.

    Для Discord gateway успешным считается получение JSON HELLO с op=10.
    """
    started = _now_ms()
    dns = resolve_probe(host, timeout=timeout)
    if not dns.get("ok"):
        return _result("websocket", False, host=host, path=path, stage="dns", dns=dns,
                       ms=round(_now_ms() - started, 1))

    sock = None
    ssock = None
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
        sock = raw
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: UmbraNet-Probe/1.0\r\n\r\n"
        ).encode("ascii", "ignore")
        ssock.sendall(req)

        response = b""
        while b"\r\n\r\n" not in response and len(response) < 8192:
            chunk = ssock.recv(1024)
            if not chunk:
                break
            response += chunk
        head = response.decode("iso-8859-1", "replace")
        first_line = head.split("\r\n", 1)[0] if head else ""
        if " 101 " not in f" {first_line} ":
            return _result(
                "websocket",
                False,
                host=host,
                path=path,
                stage="upgrade",
                first_line=first_line,
                dns=dns,
                ms=round(_now_ms() - started, 1),
            )

        opcode, payload = _read_ws_frame(ssock, timeout=timeout)
        text = payload.decode("utf-8", "replace")
        parsed = None
        op = None
        heartbeat_interval = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                op = parsed.get("op")
                data = parsed.get("d") if isinstance(parsed.get("d"), dict) else {}
                heartbeat_interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
        except Exception:
            pass
        ok = op == 10
        return _result(
            "websocket",
            ok,
            host=host,
            path=path,
            stage="hello" if ok else "frame",
            opcode=opcode,
            gateway_op=op,
            heartbeat_interval=heartbeat_interval,
            first_line=first_line,
            dns=dns,
            ms=round(_now_ms() - started, 1),
        )
    except Exception as exc:
        return _result(
            "websocket",
            False,
            host=host,
            path=path,
            stage="tls_or_ws",
            dns=dns,
            error=str(exc),
            ms=round(_now_ms() - started, 1),
        )
    finally:
        for s in (ssock, sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def probe_youtube_basic(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """YouTube media-readiness без запуска браузера.

    Проверяем не только факт открытия youtube.com, но и признаки реальной
    страницы плеера/статических ресурсов/YouTube API. Это лучше ловит ситуацию
    «главная открылась, а видео/плеер не грузится».
    """
    player = https_content_probe(
        "www.youtube.com",
        "/watch?v=dQw4w9WgXcQ&bpctr=9999999999&has_verified=1",
        # YouTube часто меняет структуру HTML/JS и может не держать старые
        # маркеры в первых чанках ответа. Поэтому для required-check важен
        # сам факт успешного GET большой watch-страницы; media/CDN проверяем
        # отдельными endpoint'ами ниже.
        contains_any=[],
        timeout=timeout,
        max_bytes=98304,
    )
    youtube_204 = https_probe("www.youtube.com", "/generate_204", "GET", timeout)
    youtubei = https_probe("youtubei.googleapis.com", "/generate_204", "GET", timeout)
    thumb = https_probe("i.ytimg.com", "/vi/dQw4w9WgXcQ/hqdefault.jpg", "GET", timeout)
    static = https_probe("s.ytimg.com", "/yts/img/favicon_32-vflOogEID.png", "GET", timeout)
    googlevideo = https_probe("redirector.googlevideo.com", "/generate_204", "GET", timeout)
    checks = [youtube_204, player, youtubei, thumb, static, googlevideo]
    ok_count = sum(1 for c in checks if c.get("ok"))

    required = {
        "youtube_204": bool(youtube_204.get("ok")),
        "player_page": bool(player.get("ok")),
        "thumbnail": bool(thumb.get("ok")),
        "googlevideo": bool(googlevideo.get("ok")),
    }
    # player_page + thumbnail обязательны: они ближе всего к реальному сценарию
    # просмотра. googlevideo может иногда быть edge-зависимым, поэтому учитываем
    # его в required/status, но общий ok допускает один частичный провал.
    ok = ok_count >= 4 and required["player_page"] and required["thumbnail"]
    return {
        "service": "youtube",
        "level": "media_readiness",
        "ok": ok,
        "score": round(ok_count / max(len(checks), 1) * 100),
        "required": required,
        "checks": checks,
    }


def probe_discord_basic(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Discord-проверка с упором на звонки.

    Вечный статус «Подключение» в Discord чаще ломается не на сайте, а на связке:
      • API/gateway WebSocket;
      • voice regions API;
      • CDN/media-домены, через которые клиент получает часть voice/media данных.

    Полностью проверить реальный звонок без аккаунта/токена невозможно, поэтому
    этот probe называется voice_readiness: он не гарантирует звонок на 100%, но
    отсекает стратегии, при которых Discord UI может открываться, а голосовая
    часть всё равно не готова.
    """
    gateway_ws = websocket_hello_probe("gateway.discord.gg", "/?v=10&encoding=json", timeout)
    voice_regions = https_probe("discord.com", "/api/v10/voice/regions", "GET", timeout)
    checks = [
        https_probe("discord.com", "/api/v10/gateway", "GET", timeout),
        voice_regions,
        https_probe("cdn.discordapp.com", "/", "HEAD", timeout),
        https_probe("media.discordapp.net", "/", "HEAD", timeout),
        https_probe("dl.discordapp.net", "/", "HEAD", timeout),
        gateway_ws,
    ]
    ok_count = sum(1 for c in checks if c.get("ok"))
    gateway_ok = bool(gateway_ws.get("ok"))
    voice_ok = bool(voice_regions.get("ok"))
    # Для звонков gateway и voice-regions обязательны. Остальные CDN/media
    # могут отдавать 403/404, но сам HTTP-ответ засчитывается как доступность пути.
    ok = ok_count >= 4 and gateway_ok and voice_ok
    return {
        "service": "discord",
        "level": "voice_readiness",
        "ok": ok,
        "score": round(ok_count / max(len(checks), 1) * 100),
        "required": {"gateway_ws": gateway_ok, "voice_regions": voice_ok},
        "checks": checks,
    }


def run_basic_probes(timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Запускает базовые probes для generation targets YouTube + Discord."""
    started = _now_ms()
    youtube = probe_youtube_basic(timeout=timeout)
    discord = probe_discord_basic(timeout=timeout)
    services = [youtube, discord]
    ok_count = sum(1 for s in services if s.get("ok"))
    return {
        "stage": "basic_probes",
        "ok": ok_count == len(services),
        "score": round(sum(int(s.get("score", 0) or 0) for s in services) / max(len(services), 1)),
        "services": services,
        "ms": round(_now_ms() - started, 1),
    }
