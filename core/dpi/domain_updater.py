"""Refresh explicitly configured remote_url domain caches, never strategy args.

Current Uz strategies use routed_domains/subscriptions, not these legacy caches.
Downloading a remote list must not silently add new DPI targets or restart WinWS.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from core.diagnostics import log_recoverable

log = logging.getLogger("UmbraNet.DomainUpdater")
_lock = threading.Lock()
MAX_BYTES = 5 * 1024 * 1024
MAX_DOMAINS = 50_000


def _parse_domains(text: str) -> list[str]:
    domains = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower().rstrip(".")
        if not line:
            continue
        # This source format is a plain hostlist, not WinWS args or a script.
        name = line.encode("idna").decode("ascii")
        labels = name.split(".")
        if (len(name) > 253 or len(labels) < 2 or
                any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels)):
            raise ValueError("Not a plain domain list")
        domains.add(name)
        if len(domains) > MAX_DOMAINS:
            raise ValueError("Too many domains")
    if not domains:
        raise ValueError("Empty domain list")
    return sorted(domains)


def update_all_strategies(strategies_dir: Path) -> bool:
    """True if all configured lists refreshed (or none configured).

    The scheduler retries False after an hour. Atomic writes preserve old files
    on network/validation failure. Manual and scheduled calls cannot overlap.
    """
    if not _lock.acquire(blocking=False):
        return False
    try:
        return _update_all(Path(strategies_dir))
    finally:
        _lock.release()


def _update_all(strategies_dir: Path) -> bool:
    ok = True
    for index, json_file in enumerate(sorted(strategies_dir.glob("*.json"))):
        if index >= 128:
            log.warning("Превышен лимит файлов стратегий для обновления")
            return False
        temporary = None
        try:
            with json_file.open("rb") as source:
                raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Strategy JSON is too large")
            data = json.loads(raw)
            remote_url = data.get("remote_url")
            if not remote_url:
                continue
            strat_id = str(data.get("id", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", strat_id):
                raise ValueError("Unsafe strategy id")
            parsed = urlsplit(str(remote_url).strip())
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                raise ValueError("Unsafe remote URL")
            chunks = []
            size = 0
            deadline = time.monotonic() + 30.0
            with requests.get(remote_url, timeout=10, stream=True) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() > deadline:
                        raise ValueError("Domain list exceeds download limits")
                    chunks.append(chunk)
            domains = _parse_domains(b"".join(chunks).decode("utf-8-sig"))
            cache_file = strategies_dir / f"remote_hostlist_{strat_id}.txt"
            content = "\n".join(domains) + "\n"
            if cache_file.exists() and cache_file.read_text(encoding="utf-8") == content:
                continue
            temporary = cache_file.with_suffix(f".{threading.get_ident()}.tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, cache_file)
            log.info("Обновлён доменный кэш стратегии: %d доменов", len(domains))
        except Exception as exc:
            ok = False
            # URL and exception text may contain subscription tokens.
            log_recoverable(log, "Не удалось обновить доменный кэш стратегии", exc,
                            level=logging.WARNING)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    log_recoverable(log, "Не удалось удалить временный hostlist", exc)
    return ok
