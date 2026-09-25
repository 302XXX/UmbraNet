"""Rate-limited diagnostics for recoverable errors, safe for DNS hot paths."""
from __future__ import annotations

import logging
import threading
import time

_lock = threading.Lock()
_last: dict[tuple[str, str], float] = {}


def log_recoverable(logger: logging.Logger, message: str, error: BaseException,
                    *, level: int = logging.DEBUG, interval: float = 60.0) -> None:
    """One record per static operation/logger per minute; never log user data.

    Exception text/tracebacks can contain subscription credentials or DNS names.
    Report the operation and exception class, not the exception payload. Callers
    must use static messages (no domains/URLs) so cardinality remains bounded.
    """
    if not logger.isEnabledFor(level):
        return
    key = (logger.name, message)
    now = time.monotonic()
    with _lock:
        previous = _last.get(key)
        if previous is not None and now - previous < interval:
            return
        _last[key] = now
    logger.log(level, "%s (%s)", message, type(error).__name__)
