def is_domain_routed(domain: str, config: dict, process_tracker=None) -> bool:
    """Проверяет, нужно ли этот домен резолвить через xbox-dns.ru.

    Логика additive (домен маршрутизируется, если выполнено ЛЮБОЕ условие):
      1) route_all включён;
      2) домен (или его поддомен) есть в config["routed_domains"];
      2.5) домен есть во внешних подписках (через быстрый O(1) поиск по суффиксам);
      3) NEW: домен недавно запрашивал процесс из config["routed_processes"]
         (определяется через process_tracker — см. process_dns_tracker.py).

    process_tracker=None по умолчанию → пункт 3 отключён, поведение полностью
    обратно совместимо (маршрутизация только по доменам).
    """
    if config.get("route_all"):
        return True

    domain = domain.rstrip('.').lower()

    # (2) по списку доменов
    for routed in config.get("routed_domains", []):
        routed = str(routed).rstrip('.').lower()
        if domain == routed or domain.endswith('.' + routed):
            return True

    # (2.5) по подпискам (высокопроизводительный O(1) поиск по суффиксам)
    sub_set = config.get("subscribed_domains_set")
    if sub_set:
        parts = domain.split(".")
        for i in range(len(parts)):
            cand = ".".join(parts[i:])
            if cand in sub_set:
                return True

    # (3) по процессам (per-app routing)
    if process_tracker is not None:
        routed_processes = config.get("routed_processes", []) or []
        if routed_processes and process_tracker.domain_requested_by(domain, routed_processes):
            return True

    return False


def is_domain_allowed(domain: str, config: dict) -> bool:
    """Проверяет пользовательский allowlist DNS-фильтра.

    Allowlist имеет приоритет над blocklist на уровне dns_server.py.
    Совпадение суффиксное: example.com разрешает и поддомены.
    """
    domain = (domain or "").rstrip('.').lower()
    if not domain:
        return False
    for allowed in config.get("allowlist_domains", []) or []:
        allowed = str(allowed).rstrip('.').lower()
        if allowed and (domain == allowed or domain.endswith('.' + allowed)):
            return True
    return False


def is_domain_blocked(domain: str, config: dict) -> bool:
    """Проверяет пользовательский DNS-блоклист.

    Совпадение суффиксное: запись example.com блокирует и сам домен,
    и поддомены вида ads.example.com.
    """
    domain = (domain or "").rstrip('.').lower()
    if not domain:
        return False
    for blocked in config.get("blocked_domains", []) or []:
        blocked = str(blocked).rstrip('.').lower()
        if blocked and (domain == blocked or domain.endswith('.' + blocked)):
            return True
    return False
