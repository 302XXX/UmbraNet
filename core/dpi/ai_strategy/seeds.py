"""
Seed templates for future AI Uz generation.

Seed — это стартовая точка генерации, а не финальная стратегия.
Генератор будет создавать временные варианты через мутации seed-параметров,
проверять их и сохранять только лучший результат как Uz.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

GOOGLE_TLS = "{bin}\\tls_clienthello_www_google_com.bin"
GOOGLE_QUIC = "{bin}\\quic_initial_www_google_com.bin"
MAX_TLS = "{bin}\\tls_clienthello_max_ru.bin"
STUN_BIN = "{bin}\\stun.bin"

SEEDS: list[dict[str, Any]] = [
    {
        "id": "minimal_tls_split",
        "title": "Minimal TLS split",
        "risk": "low",
        "priority": 10,
        "save_priority": 20,
        "description": "Диагностический мягкий seed. Не должен побеждать полноценный balanced-вариант только из-за low-risk.",
        "args": [
            "--wf-tcp=80,443,2053,2083,2087,2096,8443",
            "--filter-tcp=443",
            "{hostlist}",
            "--dpi-desync=split2",
            "--dpi-desync-split-pos=1",
        ],
    },
    {
        "id": "balanced_fake_multisplit",
        "title": "Balanced fake + multisplit",
        "risk": "medium",
        "priority": 20,
        "save_priority": 100,
        "description": "Основной рекомендуемый seed на базе рабочей Uz1-структуры.",
        "args": [
            "--wf-tcp=80,443,2053,2083,2087,2096,8443",
            "--wf-udp=443",
            "--filter-udp=443",
            "{hostlist}",
            "--dpi-desync=fake",
            "--dpi-desync-repeats=11",
            f"--dpi-desync-fake-quic={GOOGLE_QUIC}",
            "--new",
            "--filter-tcp=443",
            "{hostlist}",
            "--ip-id=zero",
            "--dpi-desync=fake,multisplit",
            "--dpi-desync-split-seqovl=681",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-repeats=8",
            f"--dpi-desync-split-seqovl-pattern={GOOGLE_TLS}",
            f"--dpi-desync-fake-tls={GOOGLE_TLS}",
            "--new",
            "--filter-tcp=80,443,8443,2053,2083,2087,2096",
            "{hostlist}",
            "--dpi-desync=fake,multisplit",
            "--dpi-desync-split-seqovl=664",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-repeats=8",
            f"--dpi-desync-split-seqovl-pattern={MAX_TLS}",
            f"--dpi-desync-fake-tls={STUN_BIN}",
            f"--dpi-desync-fake-tls={MAX_TLS}",
        ],
    },
    {
        "id": "tcp_focused_stable",
        "title": "TCP-focused stable",
        "risk": "medium",
        "priority": 30,
        "save_priority": 80,
        "description": "Seed с упором на TCP/TLS, без отдельного UDP-блока.",
        "args": [
            "--wf-tcp=80,443,2053,2083,2087,2096,8443",
            "--filter-tcp=443",
            "{hostlist}",
            "--ip-id=zero",
            "--dpi-desync=fake,multisplit",
            "--dpi-desync-split-seqovl=681",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-repeats=8",
            f"--dpi-desync-split-seqovl-pattern={GOOGLE_TLS}",
            f"--dpi-desync-fake-tls={GOOGLE_TLS}",
            "--new",
            "--filter-tcp=80,443,8443,2053,2083,2087,2096",
            "{hostlist}",
            "--dpi-desync=fake,multisplit",
            "--dpi-desync-split-seqovl=664",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-repeats=6",
            f"--dpi-desync-split-seqovl-pattern={MAX_TLS}",
            f"--dpi-desync-fake-tls={MAX_TLS}",
        ],
    },
    {
        "id": "udp_quic_probe",
        "title": "UDP/QUIC probe",
        "risk": "medium",
        "priority": 40,
        "save_priority": 35,
        "description": "Диагностический seed для проверки UDP/QUIC-пути.",
        "args": [
            "--wf-udp=443",
            "--filter-udp=443",
            "{hostlist}",
            "--dpi-desync=fake",
            "--dpi-desync-repeats=11",
            f"--dpi-desync-fake-quic={GOOGLE_QUIC}",
        ],
    },
    {
        "id": "aggressive_multisplit",
        "title": "Aggressive multisplit",
        "risk": "high",
        "priority": 50,
        "save_priority": 70,
        "description": "Более агрессивный seed для будущего глубокого подбора.",
        "args": [
            "--wf-tcp=80,443,2053,2083,2087,2096,8443",
            "--wf-udp=443",
            "--filter-udp=443",
            "{hostlist}",
            "--dpi-desync=fake",
            "--dpi-desync-repeats=11",
            f"--dpi-desync-fake-quic={GOOGLE_QUIC}",
            "--new",
            "--filter-tcp=443",
            "{hostlist}",
            "--ip-id=zero",
            "--dpi-desync=fake,multisplit",
            "--dpi-desync-split-seqovl=681",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-repeats=11",
            f"--dpi-desync-split-seqovl-pattern={GOOGLE_TLS}",
            f"--dpi-desync-fake-tls={GOOGLE_TLS}",
            "--new",
            "--filter-tcp=80,443,8443,2053,2083,2087,2096",
            "{hostlist}",
            "--dpi-desync=fake,multisplit",
            "--dpi-desync-split-seqovl=664",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-repeats=8",
            f"--dpi-desync-split-seqovl-pattern={MAX_TLS}",
            f"--dpi-desync-fake-tls={STUN_BIN}",
            f"--dpi-desync-fake-tls={MAX_TLS}",
        ],
    },
]


def list_seeds(mode: str = "quick") -> list[dict[str, Any]]:
    """Возвращает список стартовых seed-шаблонов.

    Для обычной генерации порядок не должен начинаться с минимального
    диагностического seed: иначе первые N вариантов тратятся на слабые
    стратегии, а полноценная Uz1-like база тестируется слишком поздно.
    Поэтому quick-порядок — рабочие balanced/TCP/UDP варианты, и только затем
    minimal как fallback. UI при этом остаётся простым: одна кнопка генерации.
    """
    ordered = sorted(SEEDS, key=lambda x: int(x.get("priority", 100)))
    if mode == "deep":
        return deepcopy(ordered)

    by_id = {str(x.get("id")): x for x in ordered}
    quick_ids = [
        "balanced_fake_multisplit",
        "tcp_focused_stable",
        "udp_quic_probe",
        "minimal_tls_split",
    ]
    quick = [by_id[i] for i in quick_ids if i in by_id]
    return deepcopy(quick)


def get_seed(seed_id: str) -> dict[str, Any]:
    sid = str(seed_id or "").strip().lower()
    for item in SEEDS:
        if item.get("id") == sid:
            return deepcopy(item)
    return {}


# Compatibility names for older code while the module is being migrated.
CANDIDATES = SEEDS
list_candidates = list_seeds
get_candidate = get_seed
