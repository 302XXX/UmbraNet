"""C1 guard — DPI без целей не включается."""
import tempfile
import json
import os

def test_get_dpi_targets_empty():
    import umbranet.engine_adapter as ea
    assert ea.get_dpi_targets({"routed_domains": [], "subscribed_domains_set": set()}) == []
    assert ea.count_dpi_targets({"routed_domains": [], "subscribed_domains_set": set()}) == 0

def test_get_dpi_targets_dedup_and_normalize():
    import umbranet.engine_adapter as ea
    cfg = {
        "routed_domains": ["Example.COM ", "example.com", "sub.example.com"],
        "subscribed_domains_set": {"SUB.example.com", "other.com"},
    }
    targets = ea.get_dpi_targets(cfg)
    # dedup + lower
    assert "example.com" in targets
    assert "other.com" in targets
    assert len(targets) == len(set(targets))

def test_strategy_manager_requires_hostlist():
    from strategy_manager import StrategyManager
    with tempfile.TemporaryDirectory() as tmpdir:
        m = StrategyManager(tmpdir)
        # создаём простую стратегию uz1
        data = {"id": "uz1", "name": "Uz1", "description": "", "enabled": True, "args": ["--dpi-desync=fake"], "hostlist": []}
        with open(os.path.join(tmpdir, "uz1.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        # без целей require_hostlist=True -> пусто
        args = m.get_args("uz1", routed_domains=[], require_hostlist=True)
        assert args == []
        assert "цели" in m.last_error.lower() or "hostlist" in m.last_error.lower() or "не выбраны" in m.last_error.lower()
        # с целями -> не пусто
        args2 = m.get_args("uz1", routed_domains=["example.com", "google.com"], require_hostlist=True)
        assert len(args2) > 0
        assert any("hostlist" in a for a in args2)

def test_switch_mode_guard_blocks_empty(monkeypatch):
    import umbranet.engine_adapter as ea
    # После фикса C1 переключение режимов разрешено даже без целей —
    # блокируется только Старт (серая кнопка + диалог).
    class FakeEng:
        config = {"routed_domains": [], "subscribed_domains_set": set(), "dpi_mode": "off"}
        def switch_mode(self, mode):
            return True, ""
    monkeypatch.setattr(ea, "get_engine", lambda: FakeEng())
    ok, err = ea.switch_mode("combo")
    assert ok is True
    assert err == ""

def test_switch_mode_guard_allows_with_targets(monkeypatch):
    import umbranet.engine_adapter as ea
    class FakeEng:
        config = {"routed_domains": ["example.com"], "subscribed_domains_set": set(), "dpi_mode": "off"}
        def switch_mode(self, mode):
            return True, ""
    monkeypatch.setattr(ea, "get_engine", lambda: FakeEng())
    ok, err = ea.switch_mode("combo")
    assert ok is True

def test_core_guard_blocks_empty():
    # После фикса C1 ядро тоже разрешает переключение без целей — старт заблокирует UI.
    from dns_server import UmbraNetDNS
    inst = UmbraNetDNS.__new__(UmbraNetDNS)
    inst.config = {"routed_domains": [], "subscribed_domains_set": set(), "dpi_mode": "off"}
    # заглушка set_dpi_mode — просто обновляет dict
    def fake_set(mode):
        inst.config["dpi_mode"] = mode
    inst.set_dpi_mode = fake_set  # type: ignore
    ok, err = inst.switch_mode("combo")
    assert ok is True
    assert err == ""
    # с целями — тоже проходит
    inst.config = {"routed_domains": ["example.com"], "subscribed_domains_set": set(), "dpi_mode": "off"}
    ok2, err2 = inst.switch_mode("combo")
    assert ok2 is True
    assert err2 == ""

def test_health_score_no_targets_warn(monkeypatch):
    import umbranet.engine_adapter as ea
    # подменяем get_engine и helpers чтобы health_score увидел no_dpi_targets
    class FakeEng:
        config = {
            "dpi_mode": "combo",
            "routed_domains": [],
            "subscribed_domains_set": set(),
            "dpi_strategy": "uz1",
            "xbox_dns_mode": "doh",
        }
        running = False
        winws = None
    monkeypatch.setattr(ea, "get_engine", lambda: FakeEng())
    monkeypatch.setattr(ea, "get_startup_health", lambda: {"severity": "ok", "can_start": True, "summary": "ok", "problems": [], "warnings": []})
    monkeypatch.setattr(ea, "get_current_dns_settings", lambda: {})
    monkeypatch.setattr(ea, "get_browser_doh_policies", lambda: {})
    monkeypatch.setattr(ea, "active_doh_url", lambda: "https://xbox-dns.ru/dns-query")
    monkeypatch.setattr(ea, "_winws_status_for_report", lambda: {"available": True, "running": False, "last_error": ""})
    # замокаем StrategyManager чтобы не трогать файлы
    import sys
    monkeypatch.setitem(sys.modules, "core.dpi.strategy_manager", None)
    hs = ea.health_score()
    # должен содержать проверку DPI-цели с warn
    titles = [c["title"] for c in hs["checks"]]
    assert "DPI-цели" in titles
    # score должен быть снижен за отсутствие целей
    assert hs["score"] < 100

def test_start_guard_blocks_empty():
    """Старт без целей теперь блокируется — кнопка серая."""
    import umbranet.engine_adapter as ea
    # пустой хостлист -> count == 0 -> старт нельзя
    assert ea.count_dpi_targets({"routed_domains": [], "subscribed_domains_set": set()}) == 0
    # с целью -> можно
    assert ea.count_dpi_targets({"routed_domains": ["example.com"], "subscribed_domains_set": set()}) == 1

