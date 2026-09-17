"""
Тесты оценки и мутаций AI-стратегий — `ai_strategy/scoring.py` и `mutations.py`
================================================================================

(Пункт 10 плана.)

`scoring.py` решает, какой вариант генератора сохранить. Ошибка здесь стоит
дорого: сохранится стратегия, где YouTube открывается, а Discord-звонки не
работают, — именно от этого защищает отдельный вес Discord и требование
«обязательные пробы прошли» (`ok=True`), а не только балл ≥ 70.

`mutations.py` строит варианты из seed. Он ничего не запускает и не меняет
активную стратегию: важно, чтобы мутация не портила исходный seed (иначе
следующие варианты строились бы из уже испорченного), не выходила за безопасные
границы повторов и умела убирать UDP-секции, не тронув TCP.

Запуск: python -m pytest tests/test_ai_scoring_mutations.py
"""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "core"), str(ROOT / "core" / "dpi"),
           str(ROOT / "core" / "dpi" / "ai_strategy")):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

from ai_strategy import mutations as mut
from ai_strategy import scoring
from ai_strategy.scoring import (
    choose_best,
    score_probe_result,
    score_variant,
)


def _probe(**services):
    """Проба сервисов: score_probe_samples(discord=90, youtube=40)."""
    return {"services": [{"service": name, "score": score, "ok": score >= 70}
                         for name, score in services.items()]}


# ── Оценка пробы ────────────────────────────────────────────────────────────

def test_discord_weighs_more_than_youtube():
    """Общий балл — 60 % Discord + 40 % YouTube: звонки важнее «страница открылась»."""
    result = score_probe_result(_probe(discord=90, youtube=40))
    assert result["score"] == round(90 * 0.60 + 40 * 0.40), f"получилось {result['score']}"
    assert result["score"] == 70
    assert result["services_count"] == 2


def test_other_services_are_averaged():
    """Без пары discord+youtube считается обычное среднее."""
    result = score_probe_result(_probe(twitter=80, steam=60, telegram=100))
    assert result["score"] == 80, f"среднее посчитано как {result['score']}"


def test_overall_ok_requires_all_services():
    """Общий «ок» только если ВСЕ сервисы прошли, а не большинство."""
    assert score_probe_result(_probe(discord=90, youtube=90))["ok"] is True

    mixed = score_probe_result(_probe(discord=95, youtube=20))
    assert mixed["ok"] is False, "провал YouTube признан общим успехом"
    assert mixed["ok_services"] == 1 and mixed["services_count"] == 2


def test_empty_or_broken_probe_gives_zero():
    """Пустая или битая проба — ноль и «не ок», без исключений."""
    for broken in ({}, {"services": []}, {"services": "мусор"}, None, "не словарь", []):
        result = score_probe_result(broken)
        assert result["score"] == 0, f"на входе {broken!r} получен балл {result['score']}"
        assert result["ok"] is False


def test_broken_service_entries_are_skipped():
    """Мусорные записи внутри services игнорируются, а не ломают оценку."""
    result = score_probe_result({"services": ["мусор", None, {"service": "discord", "score": 80,
                                                             "ok": True}]})
    assert result["services_count"] == 1
    assert result["service_scores"] == {"discord": 80}


# ── Оценка варианта ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("risk,penalty", [("low", 0), ("medium", 4), ("high", 10)])
def test_risk_penalty(risk, penalty):
    """Риск варианта снижает итоговый балл: предсказуемые стратегии впереди."""
    result = score_variant({"id": "v", "risk": risk, "save_priority": 50},
                           _probe(discord=90, youtube=90))
    assert result["risk_penalty"] == penalty
    assert result["score"] == 90 - penalty


def test_unknown_risk_counts_as_medium():
    """Незнакомый риск — как medium, а не «без штрафа»."""
    result = score_variant({"risk": "какой-то"}, _probe(discord=90, youtube=90))
    assert result["risk_penalty"] == scoring.RISK_PENALTY["medium"]


def test_save_priority_bonus_range():
    """Бонус за «полноценность» seed: от −5 до +5 и не больше."""
    bonus = scoring._save_priority_bonus
    assert bonus(100) == 5 and bonus(50) == 0 and bonus(0) == -5
    assert bonus(-20) == -5 and bonus(999) == 5, "значение вне диапазона не зажато"
    assert bonus("мусор") == 0, "мусор вместо приоритета не привёл к нейтральному бонусу"


def test_score_is_clamped_to_0_100():
    """Итог не выходит за 0..100 даже при большом штрафе и низком приоритете."""
    low = score_variant({"risk": "high", "save_priority": 0}, _probe(discord=3, youtube=1))
    high = score_variant({"risk": "low", "save_priority": 100}, _probe(discord=100, youtube=100))
    assert low["score"] == 0, f"балл ушёл в минус: {low['score']}"
    assert high["score"] == 100, f"балл выше 100: {high['score']}"


def test_variant_ok_requires_probes_and_threshold():
    """«Ок» варианта — это пройденные пробы И балл не ниже порога 70."""
    good = score_variant({"risk": "low", "save_priority": 50}, _probe(discord=95, youtube=95))
    assert good["ok"] is True

    failed_probe = score_variant({"risk": "low", "save_priority": 50}, _probe(discord=100, youtube=10))
    assert failed_probe["ok"] is False, "проваленная обязательная проба не помешала «ок»"

    low_score = score_variant({"risk": "low", "save_priority": 50}, _probe(discord=60, youtube=60))
    assert low_score["ok"] is False, "балл ниже порога 70 признан «ок»"


def test_variant_keeps_identification_fields():
    """В оценке сохраняются id варианта, seed и мутация — по ним потом выбирают."""
    variant = {"id": "v7", "seed_id": "balanced", "mutation": "strong_repeats",
               "mask_id": "google", "risk": "low"}
    result = score_variant(variant, _probe(discord=90, youtube=90))
    assert (result["variant_id"], result["seed_id"], result["mutation"], result["mask_id"]) == \
           ("v7", "balanced", "strong_repeats", "google")


# ── Выбор лучшего ───────────────────────────────────────────────────────────

def test_choose_best_without_variants():
    result = choose_best([])
    assert result["ok"] is False and result["reason"] == "no_variants" and result["best"] is None


def test_choose_best_prefers_high_score():
    """Побеждает вариант с большим баллом (при равных — с большим приоритетом)."""
    weak = score_variant({"id": "weak", "risk": "low", "save_priority": 50},
                         _probe(discord=80, youtube=80))
    strong = score_variant({"id": "strong", "risk": "low", "save_priority": 50},
                           _probe(discord=95, youtube=95))

    result = choose_best([weak, strong])
    assert result["ok"] is True and result["best"]["variant_id"] == "strong"


def test_choose_best_refuses_high_score_with_failed_probe():
    """Балл выше порога, но обязательная проба провалена — сохранять нельзя.

    Это ровно тот случай, ради которого появился признак ok: стратегия, где
    Discord-звонки не работают, не должна попасть в файлы только из-за балла.
    """
    # Discord — 90 (высокий вес), YouTube — 40: средневзвешенный балл ровно 70,
    # но проба YouTube провалена, значит сохранять этот вариант нельзя.
    broken = score_variant({"id": "tcponly", "risk": "low", "save_priority": 50},
                           _probe(discord=90, youtube=40))
    assert broken["score"] >= 70, "подготовка теста: балл должен быть выше порога"
    assert broken["ok"] is False, "подготовка теста: вариант должен быть «не ок»"

    result = choose_best([broken])
    assert result["ok"] is False
    assert result["reason"] == "required_probes_failed", f"причина: {result['reason']}"


def test_choose_best_below_threshold_reason():
    """Все ниже порога — причина «ниже порога», а не «пробы провалены»."""
    weak = score_variant({"id": "weak", "risk": "medium", "save_priority": 50},
                         _probe(discord=60, youtube=60))
    result = choose_best([weak])
    assert result["ok"] is False and result["reason"] == "below_threshold"


def test_choose_best_respects_custom_threshold():
    """Порог можно поднять: вариант с баллом 70 при пороге 75 уже не проходит."""
    variant = score_variant({"id": "v", "risk": "low", "save_priority": 50},
                            _probe(discord=70, youtube=70))
    assert variant["score"] == 70 and variant["ok"] is True, "подготовка теста"

    assert choose_best([variant])["ok"] is True
    strict = choose_best([variant], min_score=75)
    assert strict["ok"] is False and strict["reason"] == "below_threshold"


def test_lower_threshold_cannot_save_failed_probes():
    """Снизить порог и «протащить» вариант с проваленной пробой нельзя.

    Признак ok выставляется в score_variant и означает «обязательные пробы прошли».
    Порог в choose_best лишь дополнительная планка сверху, а не способ её обойти:
    это и защищает от сохранения стратегии, где Discord-звонки не работают.
    """
    broken = score_variant({"id": "broken", "risk": "low", "save_priority": 50},
                           _probe(discord=90, youtube=40))
    result = choose_best([broken], min_score=10)

    assert result["ok"] is False, "порог 10 пропустил вариант с проваленной пробой"
    assert result["reason"] == "required_probes_failed"


# ── Мутации: повторы ────────────────────────────────────────────────────────

def test_clamp_keeps_repeats_in_safe_range():
    assert mut._clamp(0) == mut.MIN_REPEATS
    assert mut._clamp(999) == mut.MAX_REPEATS
    assert mut._clamp(7) == 7


def test_repeats_are_shifted_by_delta():
    args = ["--wf-tcp=443", "--dpi-desync-repeats=8", "--dpi-desync=fake"]
    assert mut._replace_repeats(args, delta=-2)[1] == "--dpi-desync-repeats=6"
    assert mut._replace_repeats(args, delta=+2)[1] == "--dpi-desync-repeats=10"
    assert mut._replace_repeats(args, fixed=4)[1] == "--dpi-desync-repeats=4"


def test_repeats_are_clamped_at_bounds():
    """Даже при большом сдвиге повторы остаются в безопасном диапазоне."""
    args = ["--dpi-desync-repeats=15"]
    assert mut._replace_repeats(args, delta=+10)[0] == f"--dpi-desync-repeats={mut.MAX_REPEATS}"
    args = ["--dpi-desync-repeats=1"]
    assert mut._replace_repeats(args, delta=-10)[0] == f"--dpi-desync-repeats={mut.MIN_REPEATS}"


def test_broken_repeats_value_is_replaced_not_crash():
    """Испорченное значение повторов (не число) заменяется, а не валит мутацию."""
    args = ["--dpi-desync-repeats=много"]
    result = mut._replace_repeats(args, delta=-2)
    assert result[0].startswith("--dpi-desync-repeats="), f"получилось {result}"
    assert result[0].split("=")[1].isdigit()


def test_repeats_do_not_touch_other_args():
    args = ["--wf-tcp=443", "--dpi-desync=fake", "--dpi-desync-fake-tls=1.bin"]
    assert mut._replace_repeats(args, delta=2) == args, "мутация повторов задела чужие аргументы"


# ── Мутации: подмена bin-файлов маски ───────────────────────────────────────

def test_fake_bins_are_replaced_by_mask():
    """Маска сервиса подставляет свои TLS/QUIC-профили."""
    args = ["--dpi-desync-fake-tls=profile.bin", "--dpi-desync-fake-quic=quic.bin",
            "--dpi-desync-split-seqovl-pattern=pattern.bin", "--wf-tcp=443"]
    result = mut._replace_fake_bins(args, "google")

    assert all("google" in a for a in result[:3]), f"подстановка не сработала: {result}"
    assert result[3] == "--wf-tcp=443", "мутация задела не относящийся к делу аргумент"


def test_unknown_mask_leaves_args_untouched():
    """Неизвестная маска ничего не портит: аргументы остаются как были."""
    args = ["--dpi-desync-fake-tls=profile.bin"]
    assert mut._replace_fake_bins(args, "нет-такой") == args


def test_mask_without_quic_keeps_quic_args():
    """У маски нет QUIC-профиля — QUIC-аргумент не трогаем (не подменяем на пустоту)."""
    args = ["--dpi-desync-fake-quic=quic.bin"]
    result = mut._replace_fake_bins(args, "stun")          # у stun только tls
    assert result == args, f"QUIC-профиль испорчен: {result}"


# ── Мутации: удаление UDP-секций ────────────────────────────────────────────

def test_udp_sections_are_removed_tcp_kept():
    """TCP-only проверка: секции с UDP/QUIC убираются, TCP-секции остаются."""
    args = [
        "--wf-tcp=443", "--wf-udp=443",
        "--filter-tcp=443", "--dpi-desync=fake",
        "--new",
        "--filter-udp=443", "--dpi-desync=fake",
        "--new",
        "--filter-tcp=80", "--dpi-desync=split",
    ]
    result = mut._remove_udp_blocks(args)

    assert "--filter-udp=443" not in result, "UDP-секция осталась"
    assert "--wf-udp=443" not in result, "глобальный UDP-фильтр остался"
    assert "--wf-tcp=443" in result, "глобальный TCP-фильтр потерян"
    assert "--filter-tcp=443" in result and "--filter-tcp=80" in result
    assert result.count("--new") == 1, f"разделители секций собраны неверно: {result}"


def test_udp_only_strategy_loses_everything_but_tcp_globals():
    """Если кроме UDP ничего не было — остаются хотя бы глобальные TCP-фильтры."""
    result = mut._remove_udp_blocks(["--wf-tcp=443", "--filter-udp=443", "--dpi-desync=fake"])
    assert result == ["--wf-tcp=443"], f"получилось {result}"


def test_tcp_sections_with_both_filters_survive():
    """Секция с TCP-фильтром и UDP-аргументом не удаляется: она про TCP."""
    args = ["--filter-tcp=443", "--filter-udp=443", "--dpi-desync=fake"]
    assert mut._remove_udp_blocks(args) == args


# ── mutate_seed и generate_variants ─────────────────────────────────────────

def _seed():
    return {
        "id": "balanced", "title": "Balanced", "risk": "medium", "priority": 20,
        "save_priority": 100, "description": "описание",
        "args": ["--wf-tcp=443", "--dpi-desync-repeats=8", "--dpi-desync=fake"],
    }


def test_base_mutation_keeps_args():
    variant = mut.mutate_seed(_seed(), "base")
    assert variant["args"] == _seed()["args"]
    assert variant["id"] == "balanced__base"
    assert variant["mask_id"] == "seed_default"


def test_mutation_does_not_spoil_seed():
    """Мутация не меняет исходный seed: иначе следующие варианты строились бы из мусора."""
    seed = _seed()
    original = copy.deepcopy(seed)

    mut.mutate_seed(seed, "strong_repeats")
    mut.mutate_seed(seed, "tcp_only")
    mut.mutate_seed(seed, "base", mask_id="google")

    assert seed == original, "мутация изменила исходный seed"


def test_mutation_id_contains_mask():
    variant = mut.mutate_seed(_seed(), "soft_repeats", mask_id="Google")
    assert variant["id"] == "balanced__soft_repeats_google", f"id: {variant['id']}"
    assert variant["mask_id"] == "google", "маска не приведена к нижнему регистру"


def test_variant_carries_seed_metadata():
    variant = mut.mutate_seed(_seed(), "base")
    assert variant["seed_id"] == "balanced"
    assert variant["title"] == "Balanced"
    assert variant["risk"] == "medium"
    assert variant["save_priority"] == 100
    assert variant["args_count"] == len(variant["args"])
    assert variant["meta"]["seed_priority"] == 20


def test_unknown_mutation_is_noop():
    """Неизвестная мутация не портит аргументы (вариант равен base)."""
    variant = mut.mutate_seed(_seed(), "какая-то-мутация")
    assert variant["args"] == _seed()["args"]


def test_generate_variants_quick_mode():
    """Быстрый режим даёт варианты всех видов мутаций и не повторяется."""
    variants = mut.generate_variants(mode="quick")

    assert variants, "быстрый режим не дал вариантов"
    signatures = [tuple(v["args"]) for v in variants]
    assert len(signatures) == len(set(signatures)), "есть варианты с одинаковыми аргументами"
    found = {v["mutation"] for v in variants}
    assert {"base", "soft_repeats", "strong_repeats", "tcp_only"} <= found, f"не все мутации: {found}"


def test_generate_variants_respects_limit():
    """Лимит вариантов соблюдается — генерация не уходит в бесконечный перебор."""
    variants = mut.generate_variants(mode="deep", max_variants=12)
    assert len(variants) <= 12, f"вариантов {len(variants)} при лимите 12"


def test_generate_variants_deep_mode_adds_mutations():
    """Глубокий режим добавляет мутации повторов и маску stun."""
    variants = mut.generate_variants(mode="deep")
    found = {v["mutation"] for v in variants}
    assert {"low_repeats", "high_repeats"} <= found, f"нет мутаций глубокого режима: {found}"
    assert "stun" in {v["mask_id"] for v in variants}, "маска stun не использована"


def test_generate_variants_uses_requested_masks():
    variants = mut.generate_variants(mode="quick", masks=["yandex"])
    masks = {v["mask_id"] for v in variants}
    assert "yandex" in masks, f"запрошенная маска не применена: {masks}"
    assert "google" not in masks, f"применены лишние маски: {masks}"
