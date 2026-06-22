def ordered_upstreams(qtype: str, ipv4_servers, ipv6_servers):
    """
    Явный dual-stack порядок перебора апстримов.
    Для AAAA сначала пробуем IPv6, затем IPv4.
    Для остальных типов — сначала IPv4, затем IPv6.
    """
    v4 = [("IPv4", ip) for ip in ipv4_servers if ip]
    v6 = [("IPv6", ip) for ip in ipv6_servers if ip]
    return (v6 + v4) if qtype == "AAAA" else (v4 + v6)



def cap_response_ttl(response, ttl_cap: int):
    """Ограничивает TTL во всех секциях ответа заданным максимумом."""
    if response is None or ttl_cap is None:
        return response

    ttl_cap = max(0, int(ttl_cap))
    for section in (response.rr, response.auth, response.ar):
        for rr in section:
            ttl = int(getattr(rr, "ttl", 0) or 0)
            rr.ttl = min(ttl, ttl_cap) if ttl > 0 else ttl_cap
    return response
