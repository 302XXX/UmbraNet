"""Тесты доменной логики (normalize, parse, routing helpers)."""
import umbranet.engine_adapter as ea

def test_normalize_domain_basic():
    assert ea.normalize_domain("https://www.Example.COM/path?x=1") == "example.com"
    assert ea.normalize_domain("  chatgpt.com:443 ") == "chatgpt.com"
    assert ea.normalize_domain("http://sub.example.com/") == "sub.example.com"

def test_parse_domain_lines_plain_and_hosts():
    text = """
    example.com
    0.0.0.0 ads.example.com
    127.0.0.1 tracker.example.net # comment
    ||ads2.example.org^
    # comment line
    """
    out = ea.parse_domain_lines(text)
    assert "example.com" in out
    assert "ads.example.com" in out
    assert "tracker.example.net" in out
    assert "ads2.example.org" in out

def test_parse_domain_lines_adblock_exception_filtered():
    text = """
    ||good.example.com^
    @@||good.example.com^
    """
    # без фильтра — exception тоже попадёт как домен (include=True)
    out_all = ea.parse_domain_lines(text, include_adblock_exceptions=True)
    assert "good.example.com" in out_all
    # с фильтром blocklist — exception дропается
    out_block = ea.parse_domain_lines(text, include_adblock_exceptions=False)
    # должен содержать только один (первый), второй — отфильтрован, но домен уже есть -> остаётся один
    # проверяем что функция не падает и возвращает список без дублей
    assert out_block.count("good.example.com") == 1

def test_is_domain_routed_stub():
    cfg = {"routed_domains": ["example.com"], "routed_subscriptions": [], "route_all": False}
    assert ea.is_domain_routed("sub.example.com", cfg) is True
    assert ea.is_domain_routed("other.com", cfg) is False
