"""
Generation targets for UmbraNet AI strategies.

Важно: generation targets — это НЕ главное меню и НЕ полный список того,
что пользователь хочет разблокировать. Это стабильная база, на которой будущий
автотюнер будет проверять качество DPI-метода.

Главное меню остаётся отдельным hostlist: к нему итоговая Uz будет применяться,
но автоподбор не обязан строиться на каждом выбранном сервисе.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from core.service_profiles import (
        service_generation_domains,
        service_label,
        service_probe_family,
        service_required_domains,
    )
except Exception:  # pragma: no cover - когда core/ добавлен в sys.path как корень
    try:
        from service_profiles import (  # type: ignore
            service_generation_domains,
            service_label,
            service_probe_family,
            service_required_domains,
        )
    except Exception:
        service_generation_domains = None
        service_label = None
        service_probe_family = None
        service_required_domains = None

DATA_DIR = Path(__file__).resolve().parent / "data"
GENERATION_TARGETS_FILE = DATA_DIR / "generation_targets.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clean_domain(value: str) -> str:
    """Нормализует домен для hostlist-подобных списков."""
    d = str(value or "").strip().lower()
    if not d or d.startswith("#"):
        return ""
    if d.startswith("||"):
        d = d[2:]
    d = d.strip("^*/ ")
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0].strip().strip(".")
    return d if d and "." in d else ""


def unique_domains(domains: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in domains or []:
        d = clean_domain(str(raw))
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def load_generation_targets() -> dict[str, Any]:
    """Загружает generation targets и накладывает единые service profiles.

    JSON оставляем как декларацию default-целей/метаданных, но домены YouTube и
    Discord берём из core/service_profiles.py. Так главное меню и генератор не
    расходятся: выбранный Discord включает тот же пакет доменов, на котором
    строится hostlist для проверки временных DPI-стратегий.
    """
    data = _load_json(GENERATION_TARGETS_FILE)
    data.setdefault("default", ["youtube", "discord"])
    targets = data.setdefault("targets", {})
    if isinstance(targets, dict) and service_generation_domains:
        for sid in ("youtube", "discord"):
            item = targets.setdefault(sid, {})
            if not isinstance(item, dict):
                item = {}
                targets[sid] = item
            domains = service_generation_domains(sid) or list(item.get("domains", []) or [])
            required = service_required_domains(sid) if service_required_domains else []
            item["label"] = service_label(sid) if service_label else item.get("label", sid)
            item["domains"] = unique_domains(domains)
            item["required"] = unique_domains(required or item.get("required", []) or [])
            item["dpi_testable"] = bool(item.get("dpi_testable", True))
            item["requires_ip_change"] = bool(item.get("requires_ip_change", False))
            item["probe_family"] = (
                service_probe_family(sid, item.get("probe_family", "generic"))
                if service_probe_family else item.get("probe_family", "generic")
            )
    return data


def default_generation_target_ids(data: dict[str, Any] | None = None) -> list[str]:
    data = data or load_generation_targets()
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    ids: list[str] = []
    for tid in data.get("default", []) or []:
        tid = str(tid).strip().lower()
        if tid and tid in targets and tid not in ids:
            ids.append(tid)
    return ids


def generation_target_domains(target_ids: list[str] | None = None,
                              data: dict[str, Any] | None = None) -> list[str]:
    data = data or load_generation_targets()
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    ids = target_ids or default_generation_target_ids(data)
    domains: list[str] = []
    for tid in ids:
        item = targets.get(str(tid).strip().lower(), {})
        if isinstance(item, dict):
            domains.extend(item.get("domains", []) or [])
    return unique_domains(domains)


def collect_main_menu_domains(config: dict[str, Any] | None) -> list[str]:
    """Собирает текущие домены главного меню/подписок без влияния на generation targets."""
    cfg = config or {}
    domains: list[str] = []
    domains.extend(cfg.get("routed_domains", []) or [])
    # В рантайме subscribed_domains_set может быть set, а в config — список/отсутствовать.
    domains.extend(list(cfg.get("subscribed_domains_set", []) or []))
    domains.extend(cfg.get("subscribed_domains", []) or [])
    return unique_domains(domains)


def analyze_generation_targets(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Возвращает технический анализ базы автоподбора.

    Сейчас автоподбор строится на default generation targets: YouTube + Discord.
    Выбор пользователя в главном меню фиксируется отдельно как итоговый hostlist,
    но не заменяет базу генерации.
    """
    data = load_generation_targets()
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    ids = default_generation_target_ids(data)
    generation_domains = generation_target_domains(ids, data)
    main_menu_domains = collect_main_menu_domains(config)

    coverage: dict[str, Any] = {}
    for tid in ids:
        item = targets.get(tid, {}) if isinstance(targets.get(tid), dict) else {}
        domains = unique_domains(item.get("domains", []) or [])
        required = unique_domains(item.get("required", []) or [])
        missing_required = [d for d in required if d not in domains]
        coverage[tid] = {
            "label": item.get("label", tid),
            "domains_count": len(domains),
            "required": required,
            "missing_required": missing_required,
            "dpi_testable": bool(item.get("dpi_testable", True)),
            "requires_ip_change": bool(item.get("requires_ip_change", False)),
            "probe_family": item.get("probe_family", "generic"),
        }

    return {
        "schema_version": int(data.get("schema_version", 1) or 1),
        "source": "generation_defaults",
        "generation_target_ids": ids,
        "generation_domains": generation_domains,
        "generation_domains_count": len(generation_domains),
        "main_menu_domains": main_menu_domains,
        "main_menu_domains_count": len(main_menu_domains),
        "coverage": coverage,
    }
