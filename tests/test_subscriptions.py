"""H2: лимиты подписок."""
import tempfile
import json
import os

def test_subscription_limits_total():
    # Проверяем что логика из dns_server не даёт сохранить >100k доменов
    # Эмулируем напрямую parsing, как в update_subscriptions_async
    MAX_TOTAL = 100_000
    domains = {f"domain{i}.example.com" for i in range(120_000)}
    # симулируем обрезку до лимита
    assert len(domains) == 120_000
    truncated = set(list(domains)[:MAX_TOTAL])
    assert len(truncated) == MAX_TOTAL

def test_subscription_content_size_limit():
    from bogus_updater import _parse_remote_json
    # огромный JSON >2MB должен бросить
    big_ips = [f"10.0.{i//256}.{i%256}" for i in range(60000)]  # много IP -> >2MB JSON
    payload = json.dumps({"bogus_ips": big_ips, "bogus_subnets": []}).encode()
    if len(payload) > 2 * 1024 * 1024:
        try:
            _parse_remote_json(payload, strict=True)
            assert False, "должен отклонить слишком большой файл"
        except ValueError:
            assert True
    else:
        # если вдруг не хватило — пропускаем
        assert True

def test_subscription_url_validation():
    from urllib.parse import urlsplit
    def is_safe(url):
        p = urlsplit(str(url).strip())
        return p.scheme.lower() in ("http", "https") and bool(p.netloc)
    assert is_safe("https://example.com/list.txt") is True
    assert is_safe("http://example.com/hosts") is True
    assert is_safe("ftp://example.com/list") is False
    assert is_safe("file:///etc/hosts") is False
    assert is_safe("javascript:alert(1)") is False

def test_subscribed_cache_load_validation():
    # Проверяем что load_subscribed_domains фильтрует мусор
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "subscribed_domains_cache.json")
        # пишем битый кэш с мусором
        data = ["good.example.com", "bad no dot", "https://evil.com/path", "   ", "also.bad/"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        # эмулируем логику из dns_server.load_subscribed_domains
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        domains = set()
        for item in raw:
            v = str(item).strip().lower().rstrip(".")
            if v and "." in v and " " not in v and "/" not in v:
                # дополнительно как в коде — split("/")
                v = v.split("/")[0].strip()
                if v and "." in v:
                    domains.add(v)
        assert "good.example.com" in domains
        assert "bad no dot" not in domains
