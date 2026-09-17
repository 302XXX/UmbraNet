"""
Тесты сборки аргументов для winws.exe — `core/dpi/strategy_manager.get_args` (пункт 10).
========================================================================================

`get_args()` — единственное место, где стратегия из JSON превращается в реальную
командную строку движка: подстановка списка целей (hostlist), служебных путей и
проверка, что все плейсхолдеры развёрнуты. Ошибка здесь означает «Старт» нажали,
а DPI молча не поднялся, поэтому проверяем не только удачный путь, но и отказы с
внятными причинами: стратегия не найдена, args пустой или не список, цели не
выбраны, плейсхолдер не разрешился.

Запуск: python -m pytest tests/test_strategy_manager.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dpi")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

from strategy_manager import StrategyManager


def _write_strategy(directory: pathlib.Path, name: str, payload) -> pathlib.Path:
    path = directory / f"{name}.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def manager(tmp_path):
    """Менеджер со своей папкой стратегий (проект не трогаем)."""
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "lists").mkdir()
    mgr = StrategyManager(strategies_dir=strategies)
    return mgr


def _simple(mgr, args=None, **extra):
    payload = {"id": "uz1", "name": "Uz1", "args": args if args is not None else
               ["--wf-tcp=443", "--dpi-desync=fake"], **extra}
    _write_strategy(mgr.strategies_dir, "uz1", payload)
    return payload


# ── Поиск стратегии ─────────────────────────────────────────────────────────

def test_unknown_strategy_reports_error(manager):
    """Неизвестная стратегия: пустой список и понятная причина (а не тихий отказ)."""
    assert manager.get_args("нет-такой") == []
    assert "не найдена" in manager.last_error


def test_strategy_id_lookup_ignores_case(manager):
    _simple(manager)
    assert manager.get_args("UZ1", routed_domains=None), "поиск стратегии чувствителен к регистру"


def test_broken_json_does_not_break_manager(manager):
    """Битый файл стратегии не должен валить список остальных."""
    _write_strategy(manager.strategies_dir, "broken", "{это не json")
    _simple(manager)

    ids = [s["id"] for s in manager.list_strategies()]
    assert ids == ["uz1"], f"сломанный файл попал в список: {ids}"
    assert manager.get_args("uz1", routed_domains=None), "рабочая стратегия перестала собираться"


# ── Проверки args ───────────────────────────────────────────────────────────

def test_args_must_be_a_list(manager):
    """args строкой вместо списка — отказ с объяснением."""
    _write_strategy(manager.strategies_dir, "bad",
                    {"id": "bad", "name": "Bad", "args": "--dpi-desync=fake"})
    assert manager.get_args("bad", routed_domains=None) == []
    assert "списком" in manager.last_error


def test_empty_args_are_rejected(manager):
    _simple(manager, args=[])
    assert manager.get_args("uz1", routed_domains=None) == []
    assert "пустая" in manager.last_error


def test_args_are_trimmed_and_empty_dropped(manager):
    """Пробелы по краям убираются, пустые элементы не попадают в командную строку."""
    _simple(manager, args=["  --wf-tcp=443  ", "", "   ", "\t--dpi-desync=fake"])
    args = manager.get_args("uz1", routed_domains=None)
    assert args == ["--wf-tcp=443", "--dpi-desync=fake"], f"получилось {args}"


def test_legacy_split2_fake_order_is_fixed(manager):
    """Старый порядок split2,fake заменяется на рабочий fake,split2."""
    _simple(manager, args=["--dpi-desync=split2,fake", "--dpi-desync=fake,split2"])
    args = manager.get_args("uz1", routed_domains=None)
    assert args == ["--dpi-desync=fake,split2", "--dpi-desync=fake,split2"], f"получилось {args}"


def test_last_error_is_cleared_between_calls(manager):
    """После неудачи и успеха причина сбрасывается: в окне не остаётся старой ошибки."""
    assert manager.get_args("нет-такой") == []
    assert manager.last_error
    _simple(manager)
    assert manager.get_args("uz1", routed_domains=None)
    assert manager.last_error == "", f"осталась старая ошибка: {manager.last_error}"


# ── Список целей (hostlist) ─────────────────────────────────────────────────

def test_hostlist_is_written_and_injected(manager):
    """Цели из главного меню пишутся в файл, а его путь — в аргументы."""
    _simple(manager, args=["--dpi-desync=fake"])
    args = manager.get_args("uz1", routed_domains=["Example.com", "twitter.com"])

    hostlist = manager.active_hostlist_path
    assert hostlist.exists(), "файл со списком целей не создан"
    assert hostlist.read_text(encoding="utf-8") == "example.com\ntwitter.com\n"
    assert args[0] == f"--hostlist={hostlist.absolute()}", f"первый аргумент: {args[0]}"
    assert args[1:] == ["--dpi-desync=fake"]
    assert manager.last_hostlist_count == 2


def test_hostlist_is_repeated_before_each_section(manager):
    """winws читает аргументы посекционно: после --new список целей нужен снова."""
    _simple(manager, args=["--dpi-desync=fake", "--new", "--dpi-desync=split"])
    args = manager.get_args("uz1", routed_domains=["example.com"])

    host = args[0]
    assert host.startswith("--hostlist=")
    assert args == [host, "--dpi-desync=fake", "--new", host, "--dpi-desync=split"], (
        f"разложилось неверно: {args}"
    )


def test_hostlist_not_added_without_domains(manager):
    """Без переданного списка целей hostlist не подставляется (режим «как есть»)."""
    _simple(manager, args=["--dpi-desync=fake"])
    args = manager.get_args("uz1", routed_domains=None)
    assert args == ["--dpi-desync=fake"], f"лишний hostlist: {args}"
    assert manager.last_hostlist_count == 0


def test_empty_domains_with_require_hostlist_is_refused(manager):
    """require_hostlist и пустые цели — честный отказ: DPI без целей бессмыслен."""
    _simple(manager)
    assert manager.get_args("uz1", routed_domains=[], require_hostlist=True) == []
    assert "не выбраны цели" in manager.last_error


def test_empty_domains_without_requirement_are_ok(manager):
    _simple(manager, args=["--dpi-desync=fake"])
    args = manager.get_args("uz1", routed_domains=[], require_hostlist=False)
    assert args == ["--dpi-desync=fake"], f"получилось {args}"


def test_hostlist_file_is_removed_when_domains_disappear(manager):
    """Список целей опустел — файл удаляется, чтобы движок не читал старьё."""
    _simple(manager)
    manager.get_args("uz1", routed_domains=["example.com"])
    assert manager.active_hostlist_path.exists()

    manager.get_args("uz1", routed_domains=[])
    assert not manager.active_hostlist_path.exists(), "остался файл со старым списком целей"


def test_hostlist_is_not_rewritten_when_unchanged(manager):
    """Тот же список целей — файл не переписывается (лишние записи на диск ни к чему)."""
    _simple(manager)
    manager.get_args("uz1", routed_domains=["example.com"])
    stamp = os.stat(manager.active_hostlist_path).st_mtime_ns

    manager.get_args("uz1", routed_domains=["EXAMPLE.com", "example.com"])
    assert os.stat(manager.active_hostlist_path).st_mtime_ns == stamp, "файл переписан без нужды"


def test_domains_are_cleaned(manager):
    """Мусор в списке целей: комментарии, маски, схемы, дубли, не-домены."""
    _simple(manager)
    manager.get_args("uz1", routed_domains=[
        "# комментарий", "||twitter.com^", "https://www.youtube.com/watch?v=1",
        "  Example.COM.  ", "twitter.com", "localhost", "", "нет-точки",
    ])

    content = manager.active_hostlist_path.read_text(encoding="utf-8").splitlines()
    assert content == ["twitter.com", "www.youtube.com", "example.com", "нет-точки"] or \
           content == ["twitter.com", "www.youtube.com", "example.com"], (
        f"список целей разобран не так: {content}"
    )
    assert "localhost" not in content, "в список целей попал не-домен"
    assert "#" not in content[0], "комментарий попал в список целей"


# ── Плейсхолдеры ────────────────────────────────────────────────────────────

def test_hostlist_placeholder_is_substituted(manager):
    """Стратегия может сама указать, куда вставить список целей."""
    _simple(manager, args=["{hostlist}", "--dpi-desync=fake"])
    args = manager.get_args("uz1", routed_domains=["example.com"])

    assert args[0].startswith("--hostlist="), f"плейсхолдер не развернулся: {args[0]}"
    assert args[1] == "--dpi-desync=fake"
    assert not any("{" in a for a in args), f"остались фигурные скобки: {args}"


def test_placeholder_requires_domains(manager):
    """Стратегия с {hostlist} без целей — отказ (движку нечего фильтровать)."""
    _simple(manager, args=["{hostlist}", "--dpi-desync=fake"])
    assert manager.get_args("uz1", routed_domains=[]) == []
    assert "требует hostlist" in manager.last_error


def test_bin_and_lists_placeholders_are_absolute(manager):
    """{bin} и {lists} разворачиваются в абсолютные пути рядом со стратегиями."""
    _simple(manager, args=["--wf-tcp=443", "{bin}\\winws.exe", "--hostlist={lists}\\list.txt"])
    args = manager.get_args("uz1", routed_domains=None)

    bin_dir = str((manager.strategies_dir.parent / "bin").absolute())
    lists_dir = str((manager.strategies_dir.parent / "lists").absolute())
    assert args[1] == f"{bin_dir}\\winws.exe", f"получилось {args[1]}"
    assert args[2] == f"--hostlist={lists_dir}\\list.txt", f"получилось {args[2]}"


def test_unknown_placeholder_is_refused(manager):
    """Незнакомый плейсхолдер — отказ: winws получил бы мусор в аргументах."""
    _simple(manager, args=["--dpi-desync=fake", "--hostlist={netrogat}"])
    assert manager.get_args("uz1", routed_domains=["example.com"]) == []
    assert "плейсхолдер" in manager.last_error
    assert "{netrogat}" in manager.last_error, "в причине нет имени проблемного плейсхолдера"


# ── Список стратегий и проверка всех ────────────────────────────────────────

def test_list_strategies_skips_disabled_and_incomplete(manager):
    """В список попадают только полные стратегии; отключённые — по флагу."""
    _simple(manager)
    _write_strategy(manager.strategies_dir, "off",
                    {"id": "uz2", "name": "Uz2", "enabled": False, "args": ["--dpi-desync=fake"]})
    _write_strategy(manager.strategies_dir, "no-name", {"id": "uz3", "args": []})

    assert [s["id"] for s in manager.list_strategies()] == ["uz1"]
    assert sorted(s["id"] for s in manager.list_strategies(enabled_only=False)) == ["uz1", "uz2"]
    assert manager.get_args("uz2", routed_domains=None), "отключённую стратегию нельзя запустить вручную"


def test_validate_all_reports_each_strategy(manager):
    """validate_all — сводка для окна: кто готов, кто нет и почему."""
    _simple(manager)
    _write_strategy(manager.strategies_dir, "empty",
                    {"id": "uz9", "name": "Uz9", "args": []})

    rows = {row["id"]: row for row in manager.validate_all()}
    assert rows["uz1"]["ok"] is True and rows["uz1"]["args_count"] > 0
    assert rows["uz9"]["ok"] is False and rows["uz9"]["error"], "нет причины отказа"
