"""
UmbraNet — Blocking Detector
============================

Безопасная диагностика типа блокировки. Этот модуль НЕ обходит блокировки и не
модифицирует трафик — он только сравнивает DNS/ TCP / TLS / QUIC-признаки и
возвращает понятную классификацию:

  • dns-blocked    — системный DNS не резолвит, защищённый DNS резолвит;
  • dns-poisoned   — системный DNS вернул известный bogus-IP провайдера;
  • tcp-blocked    — DNS нормальный, но TCP/443 не открывается;
  • tls-blocked    — TCP открыт, но TLS handshake/SNI ломается;
  • quic-blocked   — HTTPS/TCP живой, но UDP/443/QUIC выглядит недоступным;
  • ok             — явных признаков блокировки не найдено;
  • unknown        — недостаточно данных.

Идея: сначала понять, какая именно проблема у пользователя. Только после этого
имеет смысл выбирать DNS Only / Combo / DPI Only.
"""

from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass, asdict
from typing import Callable, Iterable


Verdict = str

OK: Verdict = "ok"
DNS_BLOCKED: Verdict = "dns-blocked"
DNS_POISONED: Verdict = "dns-poisoned"
TCP_BLOCKED: Verdict = "tcp-blocked"
TLS_BLOCKED: Verdict = "tls-blocked"
QUIC_BLOCKED: Verdict = "quic-blocked"
UNKNOWN: Verdict = "unknown"


@dataclass
class Check:
    key: str
    status: str  # ok | warn | fail | info
    title: str
    detail: str = ""


def normalize_domain(value: str) -> str:
    d = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if ":" in d and not d.startswith("["):
        host, port = d.rsplit(":", 1)
        if port.isdigit():
            d = host
    return d


def _dedupe_ips(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for raw in values or []:
        ip = str(raw).strip()
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def resolve_system(domain: str, timeout: float = 3.0) -> tuple[list[str], str]:
    """Системный resolve. Возвращает (ips, error)."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(domain, None)
        ips = _dedupe_ips(item[4][0] for item in infos)
        return ips, ""
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    finally:
        socket.setdefaulttimeout(old_timeout)


def resolve_doh_json(domain: str, doh_url: str, timeout: float = 4.0) -> tuple[list[str], str]:
    """Простой DoH JSON resolve. Wireformat у нас уже есть в engine_adapter."""
    if not doh_url:
        return [], "DoH URL не задан"
    try:
        import requests
        ips: list[str] = []
        for qtype in ("A", "AAAA"):
            r = requests.get(
                doh_url,
                headers={"Accept": "application/dns-json"},
                params={"name": domain, "type": qtype},
                timeout=timeout,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            for ans in data.get("Answer", []) or []:
                if ans.get("type") in (1, 28) and ans.get("data"):
                    ips.append(str(ans["data"]))
        return _dedupe_ips(ips), "" if ips else "пустой DoH-ответ"
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def probe_tcp_443(host_or_ip: str, timeout: float = 3.0) -> tuple[bool | None, str]:
    """TCP connect к 443. True=open, False=reset/error, None=timeout."""
    try:
        s = socket.create_connection((host_or_ip, 443), timeout=timeout)
        s.close()
        return True, "TCP/443 открыт"
    except socket.timeout:
        return None, "таймаут TCP/443"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def probe_tls_handshake(domain: str, timeout: float = 4.0) -> tuple[bool | None, str]:
    """TLS handshake с SNI. True=OK, False=сломался, None=timeout."""
    try:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((domain, 443), timeout=timeout)
        raw.settimeout(timeout)
        with ctx.wrap_socket(raw, server_hostname=domain):
            return True, "TLS handshake OK"
    except socket.timeout:
        return None, "таймаут TLS handshake"
    except ssl.SSLCertVerificationError as exc:
        # Это НЕ обязательно DPI. Часто bare-домен CDN (например googlevideo.com)
        # отвечает сертификатом не под этот hostname. Считаем это неоднозначным,
        # чтобы не давать ложный verdict=tls-blocked.
        return None, f"TLS есть, но сертификат не подходит: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def probe_udp_443_basic(host_or_ip: str, timeout: float = 1.5) -> tuple[bool | None, str]:
    """Fallback UDP/443 check, если aioquic недоступен."""
    try:
        family = socket.AF_INET6 if ":" in host_or_ip else socket.AF_INET
        s = socket.socket(family, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        addr = (host_or_ip, 443, 0, 0) if family == socket.AF_INET6 else (host_or_ip, 443)
        s.sendto(b"\x00", addr)
        s.close()
        return None, "UDP/443 отправлен, вывод невозможен без QUIC-probe"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def probe_quic_default(host: str) -> tuple[bool | None, str]:
    """QUIC probe через aioquic, с graceful fallback."""
    try:
        from quic_probe import probe_quic  # type: ignore
        return probe_quic(host)
    except Exception:
        return probe_udp_443_basic(host)


def recommended_mode(verdict: Verdict) -> str:
    if verdict in (DNS_BLOCKED, DNS_POISONED):
        return "dns_only"
    if verdict in (TCP_BLOCKED, TLS_BLOCKED, QUIC_BLOCKED):
        return "dpi_only"
    if verdict == OK:
        return "dns_only"
    return "unknown"


def _summary(verdict: Verdict) -> str:
    return {
        OK: "Явных признаков блокировки не найдено",
        DNS_BLOCKED: "Похоже на DNS-блокировку: защищённый DNS отвечает, системный — нет",
        DNS_POISONED: "Похоже на DNS-подмену: системный DNS вернул bogus-IP",
        TCP_BLOCKED: "DNS работает, но TCP/443 недоступен — DNS Only не поможет",
        TLS_BLOCKED: "TCP открыт, но TLS/SNI handshake ломается — похоже на DPI",
        QUIC_BLOCKED: "HTTPS/TCP живой, но UDP/443/QUIC выглядит проблемным",
        UNKNOWN: "Недостаточно данных для точного вывода",
    }.get(verdict, "Недостаточно данных")


def detect_blocking(
    domain: str,
    *,
    secure_doh_url: str = "https://cloudflare-dns.com/dns-query",
    system_resolver: Callable[[str], tuple[list[str], str]] | None = None,
    secure_resolver: Callable[[str, str], tuple[list[str], str]] | None = None,
    tcp_probe: Callable[[str], tuple[bool | None, str]] | None = None,
    tls_probe: Callable[[str], tuple[bool | None, str]] | None = None,
    quic_probe: Callable[[str], tuple[bool | None, str]] | None = None,
    bogus_checker: Callable[[str], bool] | None = None,
    quic_required: bool = False,
) -> dict:
    """Главная диагностика. Все probe-функции можно подменять в тестах."""
    d = normalize_domain(domain)
    checks: list[Check] = []
    if not d:
        return {
            "domain": d,
            "verdict": UNKNOWN,
            "severity": "error",
            "summary": "Введите домен",
            "recommended_mode": "unknown",
            "checks": [],
        }

    system_resolver = system_resolver or resolve_system
    secure_resolver = secure_resolver or resolve_doh_json
    tcp_probe = tcp_probe or probe_tcp_443
    tls_probe = tls_probe or probe_tls_handshake
    quic_probe = quic_probe or probe_quic_default
    bogus_checker = bogus_checker or (lambda _ip: False)

    t0 = time.perf_counter()
    sys_ips, sys_err = system_resolver(d)
    checks.append(Check(
        "system_dns", "ok" if sys_ips else "fail",
        "Системный DNS", ", ".join(sys_ips[:5]) if sys_ips else sys_err,
    ))

    sec_ips, sec_err = secure_resolver(d, secure_doh_url)
    checks.append(Check(
        "secure_dns", "ok" if sec_ips else "warn",
        "Защищённый DNS", ", ".join(sec_ips[:5]) if sec_ips else sec_err,
    ))

    if sys_ips and any(bogus_checker(ip) for ip in sys_ips):
        verdict = DNS_POISONED
        checks.append(Check("bogus", "fail", "Bogus-IP", "Системный DNS вернул известную заглушку провайдера"))
        return _result(d, verdict, checks, t0)

    if not sys_ips and sec_ips:
        verdict = DNS_BLOCKED
        return _result(d, verdict, checks, t0)

    target_ips = sec_ips or sys_ips
    if not target_ips:
        return _result(d, UNKNOWN, checks, t0)

    # TCP лучше проверять по IP, чтобы отделить DNS от соединения.
    first_v4 = next((ip for ip in target_ips if ":" not in ip), target_ips[0])
    tcp_ok, tcp_detail = tcp_probe(first_v4)
    checks.append(Check(
        "tcp_443", "ok" if tcp_ok is True else ("warn" if tcp_ok is None else "fail"),
        "TCP/443", tcp_detail,
    ))
    if tcp_ok is False:
        return _result(d, TCP_BLOCKED, checks, t0)

    tls_ok, tls_detail = tls_probe(d)
    checks.append(Check(
        "tls", "ok" if tls_ok is True else ("warn" if tls_ok is None else "fail"),
        "TLS/SNI", tls_detail,
    ))
    if tcp_ok is True and tls_ok is False:
        return _result(d, TLS_BLOCKED, checks, t0)

    # QUIC нужно проверять по hostname, чтобы корректно передать SNI/ALPN.
    quic_ok, quic_detail = quic_probe(d)
    checks.append(Check(
        "udp_443", "ok" if quic_ok is True else ("info" if quic_ok is None else "warn"),
        "UDP/443 / QUIC", quic_detail,
    ))
    if quic_ok is False and tcp_ok is True and quic_required:
        return _result(d, QUIC_BLOCKED, checks, t0)

    return _result(d, OK, checks, t0)


def _result(domain: str, verdict: Verdict, checks: list[Check], started: float) -> dict:
    severity = "ok" if verdict == OK else ("warning" if verdict in (UNKNOWN, QUIC_BLOCKED) else "problem")
    return {
        "domain": domain,
        "verdict": verdict,
        "severity": severity,
        "summary": _summary(verdict),
        "recommended_mode": recommended_mode(verdict),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "checks": [asdict(c) for c in checks],
    }
