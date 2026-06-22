"""
UmbraNet — service-specific blocking diagnostics.

Обычная проверка одного домена недостаточна для YouTube/Discord: главная
страница может открываться, а видео CDN / gateway / voice/media — нет.
Этот модуль группирует несколько endpoint'ов сервиса и агрегирует вердикт.
"""

from __future__ import annotations

from collections import Counter

try:
    from service_profiles import SERVICE_PROFILES as CORE_SERVICE_PROFILES
except Exception:  # pragma: no cover - при импорте как core.blocking_services
    from core.service_profiles import SERVICE_PROFILES as CORE_SERVICE_PROFILES

try:
    from blocking_detector import (
        DNS_BLOCKED,
        DNS_POISONED,
        OK,
        QUIC_BLOCKED,
        TCP_BLOCKED,
        TLS_BLOCKED,
        UNKNOWN,
        detect_blocking,
        normalize_domain,
    )
except Exception:  # pragma: no cover - при импорте как core.blocking_services
    from core.blocking_detector import (
        DNS_BLOCKED,
        DNS_POISONED,
        OK,
        QUIC_BLOCKED,
        TCP_BLOCKED,
        TLS_BLOCKED,
        UNKNOWN,
        detect_blocking,
        normalize_domain,
    )

SERVICE_PROFILES = {
    sid: {
        "label": prof.get("label", sid),
        "domains": list(prof.get("runtime_domains", []) or []),
        **({"quic_required": True} if prof.get("quic_required") else {}),
    }
    for sid, prof in CORE_SERVICE_PROFILES.items()
    if sid in ("youtube", "discord", "chatgpt")
}


PROBLEM_VERDICTS = {DNS_BLOCKED, DNS_POISONED, TCP_BLOCKED, TLS_BLOCKED, QUIC_BLOCKED}
DPI_VERDICTS = {TCP_BLOCKED, TLS_BLOCKED, QUIC_BLOCKED}
DNS_VERDICTS = {DNS_BLOCKED, DNS_POISONED}


def service_ids() -> list[str]:
    return list(SERVICE_PROFILES.keys())


def service_label(service_id: str) -> str:
    return SERVICE_PROFILES.get(service_id, {}).get("label", service_id)


def recommended_mode_for_service(verdicts: list[str]) -> str:
    has_dpi = any(v in DPI_VERDICTS for v in verdicts)
    has_dns = any(v in DNS_VERDICTS for v in verdicts)
    if has_dpi and has_dns:
        return "combo"
    if has_dpi:
        return "dpi_only"
    if has_dns:
        return "dns_only"
    if verdicts and all(v == OK for v in verdicts):
        return "dns_only"
    return "unknown"


def service_summary(service_label_text: str, verdicts: list[str], problem_count: int, total: int) -> str:
    if total <= 0:
        return f"{service_label_text}: нет endpoint'ов для проверки"
    if problem_count == 0:
        if verdicts and all(v == OK for v in verdicts):
            return f"{service_label_text}: явных признаков блокировки не найдено"
        return f"{service_label_text}: недостаточно данных для вывода"

    c = Counter(v for v in verdicts if v in PROBLEM_VERDICTS)
    if any(v in DPI_VERDICTS for v in c):
        return f"{service_label_text}: часть endpoint'ов выглядит как DPI/соединение-проблема ({problem_count}/{total})"
    if any(v in DNS_VERDICTS for v in c):
        return f"{service_label_text}: часть endpoint'ов выглядит как DNS-проблема ({problem_count}/{total})"
    return f"{service_label_text}: найдены проблемы ({problem_count}/{total})"


def detect_service_blocking(service_id: str, **detector_kwargs) -> dict:
    profile = SERVICE_PROFILES.get(service_id)
    if not profile:
        return {
            "service_id": service_id,
            "service": service_id,
            "summary": "Неизвестный сервис",
            "recommended_mode": "unknown",
            "severity": "error",
            "endpoints": [],
        }

    endpoints = []
    seen_domains = set()
    for domain in profile.get("domains", []):
        norm = normalize_domain(domain)
        if not norm or norm in seen_domains:
            continue
        seen_domains.add(norm)
        endpoints.append(detect_blocking(
            norm,
            quic_required=bool(profile.get("quic_required", False)),
            **detector_kwargs,
        ))

    verdicts = [e.get("verdict", UNKNOWN) for e in endpoints]
    problem_count = sum(1 for v in verdicts if v in PROBLEM_VERDICTS)
    total = len(endpoints)
    rec = recommended_mode_for_service(verdicts)
    severity = "ok" if problem_count == 0 and all(v == OK for v in verdicts) else (
        "warning" if problem_count == 0 else "problem"
    )
    label = profile.get("label", service_id)
    return {
        "service_id": service_id,
        "service": label,
        "summary": service_summary(label, verdicts, problem_count, total),
        "recommended_mode": rec,
        "severity": severity,
        "problem_count": problem_count,
        "total": total,
        "endpoints": endpoints,
    }
