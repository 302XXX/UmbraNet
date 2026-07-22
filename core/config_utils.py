import copy
import ipaddress
import json
import logging
import os
import time
from urllib.parse import urlsplit

from profile_utils import BUILTIN_PROFILE_ID, get_builtin_dns_profiles, sanitize_user_dns_profiles

DEFAULT_CONFIG = {
    # Как резолвить выбранные домены через xbox-dns.ru:
    #   "doh"  — через HTTPS https://xbox-dns.ru/dns-query (по умолчанию:
    #            работает по доменному имени и не ломается при смене IP сервиса)
    #   "udp"  — через UDP DNS по IP профиля (быстрее, но IP могут устареть;
    #            при отказе UDP код сам сделает fallback на DoH)
    "xbox_dns_mode": "doh",
    # UI-режим работы:
    #   off    — DNS Only: локальный DNS + маршрутизация, DPI выключен
    #   combo  — DNS + DPI combo (если pydivert/WinDivert доступны)
    #   zapret — DPI Only: DNS нужен для резолва, DPI в более агрессивном режиме
    "dpi_mode": "off",
    # Выбранный метод DPI/WinWS. Uz-стратегии — это только способ обхода;
    # список целей всегда берётся из routed_domains.
    "dpi_strategy": "uz1",
    # Проверять TLS-сертификаты для DoT/DoQ. False = шифрование без проверки имени
    # (удобно для подключения по «голому» IP, но менее строго).
    "tls_verify": True,
    # Невидимый failover между провайдерами (xbox-dns → comss.one → ...).
    # Если основной провайдер недоступен, тихо пробуем запасной, чтобы
    # пользователь не остался без доступа. True по умолчанию.
    "provider_failover": True,
    "fallback_dns": "8.8.8.8",
    "fallback_dns6": "2001:4860:4860::8888",
    "listen_port": 53,
    "listen_host": "127.0.0.1",   # IPv4 loopback
    "listen_host6": "::1",        # IPv6 loopback
    "enable_ipv6": True,
    "routed_cache_enabled": True,
    "routed_cache_ttl": 5,
    "routed_reply_ttl": 1,
    # Стратегия опроса нескольких upstream-серверов:
    #   "sequential" — по очереди (как было исторически, безопасно)
    #   "parallel"   — все одновременно, берём первый ответ (быстрее всего)
    #   "fastest"    — последовательно от выученного лидера + периодический probe
    # См. upstream_strategy.py.
    "upstream_mode": "parallel",
    # Optimistic cache (stale-while-revalidate): даёт мгновенный ответ
    # из устаревшего кэша + в фоне обновляет. См. dns_cache.py / dns_server.py.
    "optimistic_cache_enabled": True,
    "stale_cache_ttl": 3600,
    # Bogus IP detection (заглушки МТС/РТ/Билайн/Мегафон и т.п.)
    "bogus_detection_enabled": True,
    "bogus_ips_use_builtin": True,
    "bogus_ips_extra": [],
    "bogus_subnets_extra": [],
    "active_dns_profile": BUILTIN_PROFILE_ID,
    "user_dns_profiles": [],
    "allowlist_domains": [],
    "blocked_domains": [],
    "routed_domains": [
        "openai.com",
        "chatgpt.com",
        "api.openai.com",
        "auth0.openai.com",
        "cdn.oaistatic.com",
        "chat.openai.com",
        "ab.chatgpt.com",
        "files.oaiusercontent.com",
    ],
    "routed_processes": [],
    "route_all": False,
    "ipv6_priority_enabled": False,
    "routed_subscriptions": [],
}


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
    return default


def _normalize_domain(raw):
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None

    if "://" in value:
        value = urlsplit(value).netloc or urlsplit(value).path

    value = value.strip().lower()
    if value.startswith("www."):
        value = value[4:]
    value = value.split("/")[0].rstrip(".")
    return value or None


def _normalize_process(raw):
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value or None


def _validate_ipv4(value):
    try:
        addr = ipaddress.ip_address(str(value).strip())
        return str(addr) if addr.version == 4 else None
    except Exception:
        return None


def _validate_ipv6(value):
    try:
        addr = ipaddress.ip_address(str(value).strip())
        return str(addr) if addr.version == 6 else None
    except Exception:
        return None


def _validate_non_negative_int(raw_value, default_value, field_name, warnings, max_value=86400):
    try:
        value = int(raw_value)
        if 0 <= value <= max_value:
            return value
        warnings.append(f"{field_name} вне диапазона 0..{max_value}, установлено {default_value}")
    except Exception:
        warnings.append(f"{field_name} должен быть числом, установлено {default_value}")
    return default_value


def sanitize_config(raw_cfg):
    """Нормализует и валидирует конфиг, возвращая (cfg, warnings)."""
    warnings = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if not isinstance(raw_cfg, dict):
        warnings.append("Конфиг повреждён: ожидался JSON-объект, применены настройки по умолчанию")
        return cfg, warnings

    mode = str(raw_cfg.get("xbox_dns_mode", cfg["xbox_dns_mode"])).strip().lower()
    if mode in ("udp", "doh", "dot", "doq", "dnscrypt"):
        cfg["xbox_dns_mode"] = mode
    else:
        warnings.append("Некорректный xbox_dns_mode, установлен 'doh'")

    dpi_mode = str(raw_cfg.get("dpi_mode", cfg["dpi_mode"])).strip().lower()
    if dpi_mode in ("off", "combo", "zapret"):
        cfg["dpi_mode"] = dpi_mode
    else:
        cfg["dpi_mode"] = "off"
        warnings.append("Некорректный dpi_mode, установлен 'off'")

    dpi_strategy = str(raw_cfg.get("dpi_strategy", cfg["dpi_strategy"]) or "").strip().lower()
    cfg["dpi_strategy"] = dpi_strategy or cfg["dpi_strategy"]

    # Проверять ли TLS-сертификаты для DoT/DoQ (по умолчанию да).
    cfg["tls_verify"] = bool(raw_cfg.get("tls_verify", cfg.get("tls_verify", True)))
    # Failover между провайдерами (по умолчанию включён).
    cfg["provider_failover"] = bool(raw_cfg.get("provider_failover", cfg.get("provider_failover", True)))

    try:
        port = int(raw_cfg.get("listen_port", cfg["listen_port"]))
        if 1 <= port <= 65535:
            cfg["listen_port"] = port
        else:
            warnings.append("listen_port вне диапазона 1..65535, установлен 53")
    except Exception:
        warnings.append("listen_port должен быть числом, установлен 53")

    listen_host = _validate_ipv4(raw_cfg.get("listen_host", cfg["listen_host"]))
    if listen_host:
        cfg["listen_host"] = listen_host
    else:
        warnings.append("listen_host некорректен, установлен 127.0.0.1")

    listen_host6 = _validate_ipv6(raw_cfg.get("listen_host6", cfg["listen_host6"]))
    if listen_host6:
        cfg["listen_host6"] = listen_host6
    else:
        warnings.append("listen_host6 некорректен, установлен ::1")

    fallback_dns = _validate_ipv4(raw_cfg.get("fallback_dns", cfg["fallback_dns"]))
    if fallback_dns is None:
        warnings.append("fallback_dns должен быть IPv4-адресом, установлен 8.8.8.8")
        fallback_dns = cfg["fallback_dns"]
    else:
        try:
            if ipaddress.ip_address(fallback_dns).is_loopback or fallback_dns == cfg["listen_host"]:
                warnings.append("fallback_dns не должен указывать на localhost/самого себя, установлен 8.8.8.8")
                fallback_dns = cfg["fallback_dns"]
        except Exception:
            fallback_dns = cfg["fallback_dns"]
    cfg["fallback_dns"] = fallback_dns

    raw_fallback_dns6 = raw_cfg.get("fallback_dns6", cfg["fallback_dns6"])
    if raw_fallback_dns6 is None or str(raw_fallback_dns6).strip() == "":
        cfg["fallback_dns6"] = ""
    else:
        fallback_dns6 = _validate_ipv6(raw_fallback_dns6)
        if fallback_dns6 is None:
            warnings.append("fallback_dns6 должен быть IPv6-адресом или пустым, установлен 2001:4860:4860::8888")
            fallback_dns6 = DEFAULT_CONFIG["fallback_dns6"]
        else:
            try:
                if ipaddress.ip_address(fallback_dns6).is_loopback or fallback_dns6 == cfg["listen_host6"]:
                    warnings.append(
                        "fallback_dns6 не должен указывать на localhost/самого себя, установлен 2001:4860:4860::8888"
                    )
                    fallback_dns6 = DEFAULT_CONFIG["fallback_dns6"]
            except Exception:
                fallback_dns6 = DEFAULT_CONFIG["fallback_dns6"]
        cfg["fallback_dns6"] = fallback_dns6

    cfg["enable_ipv6"] = _to_bool(raw_cfg.get("enable_ipv6", cfg["enable_ipv6"]), cfg["enable_ipv6"])
    cfg["route_all"] = _to_bool(raw_cfg.get("route_all", cfg["route_all"]), cfg["route_all"])
    cfg["routed_cache_enabled"] = _to_bool(
        raw_cfg.get("routed_cache_enabled", cfg["routed_cache_enabled"]),
        cfg["routed_cache_enabled"],
    )
    cfg["routed_cache_ttl"] = _validate_non_negative_int(
        raw_cfg.get("routed_cache_ttl", cfg["routed_cache_ttl"]),
        DEFAULT_CONFIG["routed_cache_ttl"],
        "routed_cache_ttl",
        warnings,
        max_value=3600,
    )
    cfg["routed_reply_ttl"] = _validate_non_negative_int(
        raw_cfg.get("routed_reply_ttl", cfg["routed_reply_ttl"]),
        DEFAULT_CONFIG["routed_reply_ttl"],
        "routed_reply_ttl",
        warnings,
        max_value=3600,
    )

    # ── Upstream strategy ───────────────────────────────────────────────────
    raw_upstream_mode = str(raw_cfg.get("upstream_mode", cfg["upstream_mode"]) or "").strip().lower()
    if raw_upstream_mode in ("sequential", "parallel", "fastest"):
        cfg["upstream_mode"] = raw_upstream_mode
    else:
        warnings.append(f"upstream_mode некорректен, установлено '{cfg['upstream_mode']}'")

    # ── Optimistic cache (stale-while-revalidate) ───────────────────────────
    cfg["optimistic_cache_enabled"] = _to_bool(
        raw_cfg.get("optimistic_cache_enabled", cfg["optimistic_cache_enabled"]),
        cfg["optimistic_cache_enabled"],
    )
    # stale_cache_ttl: до 24 часов разрешаем (для "Скоростного" пресета).
    cfg["stale_cache_ttl"] = _validate_non_negative_int(
        raw_cfg.get("stale_cache_ttl", cfg["stale_cache_ttl"]),
        DEFAULT_CONFIG["stale_cache_ttl"],
        "stale_cache_ttl",
        warnings,
        max_value=86400,
    )

    # ── Bogus IP detection (опциональные поля, тут только bool/списки) ──────
    cfg["bogus_detection_enabled"] = _to_bool(
        raw_cfg.get("bogus_detection_enabled", cfg["bogus_detection_enabled"]),
        cfg["bogus_detection_enabled"],
    )
    cfg["bogus_ips_use_builtin"] = _to_bool(
        raw_cfg.get("bogus_ips_use_builtin", cfg["bogus_ips_use_builtin"]),
        cfg["bogus_ips_use_builtin"],
    )
    for list_field in ("bogus_ips_extra", "bogus_subnets_extra"):
        raw_list = raw_cfg.get(list_field, [])
        if isinstance(raw_list, list):
            cfg[list_field] = [str(x).strip() for x in raw_list if str(x).strip()]
        else:
            cfg[list_field] = []
            warnings.append(f"{list_field} должен быть списком, применён пустой")

    allowlist_domains_raw = raw_cfg.get("allowlist_domains", DEFAULT_CONFIG["allowlist_domains"])
    allowlist_domains = []
    seen_allowed = set()
    if isinstance(allowlist_domains_raw, list):
        for item in allowlist_domains_raw:
            domain = _normalize_domain(item)
            if domain and domain not in seen_allowed:
                seen_allowed.add(domain)
                allowlist_domains.append(domain)
        cfg["allowlist_domains"] = allowlist_domains
    else:
        cfg["allowlist_domains"] = []
        warnings.append("allowlist_domains должен быть списком, применён пустой список")

    blocked_domains_raw = raw_cfg.get("blocked_domains", DEFAULT_CONFIG["blocked_domains"])
    blocked_domains = []
    seen_blocked = set()
    if isinstance(blocked_domains_raw, list):
        for item in blocked_domains_raw:
            domain = _normalize_domain(item)
            if domain and domain not in seen_blocked:
                seen_blocked.add(domain)
                blocked_domains.append(domain)
        cfg["blocked_domains"] = blocked_domains
    else:
        cfg["blocked_domains"] = []
        warnings.append("blocked_domains должен быть списком, применён пустой список")

    routed_domains_raw = raw_cfg.get("routed_domains", DEFAULT_CONFIG["routed_domains"])
    routed_domains = []
    seen_domains = set()
    if isinstance(routed_domains_raw, list):
        for item in routed_domains_raw:
            domain = _normalize_domain(item)
            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                routed_domains.append(domain)
        if not routed_domains and routed_domains_raw:
            warnings.append("Список routed_domains содержал только некорректные значения")
        cfg["routed_domains"] = routed_domains
    else:
        warnings.append("routed_domains должен быть списком, применён список по умолчанию")
        cfg["routed_domains"] = copy.deepcopy(DEFAULT_CONFIG["routed_domains"])

    routed_processes_raw = raw_cfg.get("routed_processes", [])
    routed_processes = []
    seen_processes = set()
    if isinstance(routed_processes_raw, list):
        for item in routed_processes_raw:
            process = _normalize_process(item)
            if process and process not in seen_processes:
                seen_processes.add(process)
                routed_processes.append(process)
    else:
        warnings.append("routed_processes должен быть списком, список процессов очищен")
    cfg["routed_processes"] = routed_processes

    raw_user_profiles = raw_cfg.get("user_dns_profiles", [])
    if not isinstance(raw_user_profiles, list):
        warnings.append("user_dns_profiles должен быть списком, пользовательские DNS профили очищены")
        raw_user_profiles = []
    cfg["user_dns_profiles"] = sanitize_user_dns_profiles(raw_user_profiles)

    active_profile = str(raw_cfg.get("active_dns_profile", BUILTIN_PROFILE_ID)).strip() or BUILTIN_PROFILE_ID
    # Допустимы ВСЕ встроенные профили (xbox-dns, comss.one и т.п.), а не только
    # основной, плюс пользовательские.
    builtin_ids = {p["id"] for p in get_builtin_dns_profiles()}
    all_profile_ids = builtin_ids | {p["id"] for p in cfg["user_dns_profiles"]}
    if active_profile not in all_profile_ids:
        warnings.append("active_dns_profile не найден, выбран встроенный профиль")
        active_profile = BUILTIN_PROFILE_ID
    cfg["active_dns_profile"] = active_profile

    cfg["ipv6_priority_enabled"] = _to_bool(
        raw_cfg.get("ipv6_priority_enabled", cfg["ipv6_priority_enabled"]),
        cfg["ipv6_priority_enabled"],
    )

    raw_subs = raw_cfg.get("routed_subscriptions", [])
    if isinstance(raw_subs, list):
        cfg["routed_subscriptions"] = [str(x).strip() for x in raw_subs if str(x).strip()]
    else:
        cfg["routed_subscriptions"] = []
        warnings.append("routed_subscriptions должен быть списком")

    return cfg, warnings


def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    temp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)


def save_config_file(path, cfg, logger=None):
    logger = logger or logging.getLogger("UmbraNet.Config")
    sanitized, warnings = sanitize_config(cfg)
    for warning in warnings:
        logger.warning(warning)
    _atomic_write_json(path, sanitized)
    return sanitized


def load_config_file(path, logger=None):
    logger = logger or logging.getLogger("UmbraNet.Config")

    if not os.path.exists(path):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config_file(path, cfg, logger=logger)
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_cfg = json.load(f)
    except Exception as exc:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        broken_path = f"{path}.broken-{stamp}"
        try:
            os.replace(path, broken_path)
            logger.error(f"Конфиг повреждён, сохранён backup: {broken_path}")
        except Exception:
            logger.error("Конфиг повреждён, не удалось сохранить backup")
        logger.error(f"Ошибка чтения config.json: {exc}. Загружаются настройки по умолчанию")
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config_file(path, cfg, logger=logger)
        return cfg

    sanitized, warnings = sanitize_config(raw_cfg)
    for warning in warnings:
        logger.warning(warning)

    if sanitized != raw_cfg:
        save_config_file(path, sanitized, logger=logger)
        logger.info("config.json был нормализован и пересохранён")

    return sanitized
