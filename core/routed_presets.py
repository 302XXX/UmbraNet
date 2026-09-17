"""
Пресеты режимов UmbraNet.

Каждый пресет управляет двумя группами настроек:

  1) Routed-кэш (для маршрутизируемых доменов через xbox-dns/DoH):
        routed_cache_enabled, routed_cache_ttl, routed_reply_ttl

  2) Optimistic cache (для всех доменов, stale-while-revalidate):
        optimistic_cache_enabled, stale_cache_ttl

В UI пресет ставится одной кнопкой; если потом пользователь меняет
любое из этих 5 полей вручную — название пресета сменится на «Пользовательский».
"""

ROUTED_PRESETS = [
    (
        "Совместимый",
        {
            # routed-кэш отключён, всё всегда заново
            "routed_cache_enabled": False,
            "routed_cache_ttl": 0,
            "routed_reply_ttl": 0,
            # optimistic тоже выключаем — в этом режиме приоритет максимальной
            # свежести ответов, никаких "просроченных, но быстро" не отдаём.
            "optimistic_cache_enabled": False,
            "stale_cache_ttl": 0,
        },
        "Всегда максимально свежий DNS, без кэшей",
    ),
    (
        "Рекомендуемый",
        {
            "routed_cache_enabled": True,
            "routed_cache_ttl": 5,
            "routed_reply_ttl": 1,
            # Optimistic включён с умеренным окном: 1 час
            # — большинство сайтов получит мгновенный ответ из кэша,
            # а в фоне он будет обновляться.
            "optimistic_cache_enabled": True,
            "stale_cache_ttl": 3600,
        },
        "Баланс свежести и скорости, фоновое обновление кэша",
    ),
    (
        "Скоростной",
        {
            "routed_cache_enabled": True,
            "routed_cache_ttl": 30,
            "routed_reply_ttl": 30,
            # Максимальный stale-горизонт: 24 часа.
            # DNS-запросы наружу почти не уходят, всё летает из памяти.
            "optimistic_cache_enabled": True,
            "stale_cache_ttl": 86400,
        },
        "Минимум запросов наружу, максимум кэша (до 24 ч)",
    ),
]


# Поля, по которым определяется «активный» пресет. Если хоть одно отличается —
# пользователь считается «Пользовательским».
_PRESET_FIELDS_BOOL = ("routed_cache_enabled", "optimistic_cache_enabled")
_PRESET_FIELDS_INT = ("routed_cache_ttl", "routed_reply_ttl", "stale_cache_ttl")


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def get_routed_preset_name(
    routed_cache_enabled,
    routed_cache_ttl,
    routed_reply_ttl,
    optimistic_cache_enabled=None,
    stale_cache_ttl=None,
):
    """Возвращает имя пресета, который сейчас активен.

    Параметры optimistic_cache_enabled и stale_cache_ttl сделаны опциональными
    (с дефолтами из «Рекомендуемого»), чтобы старые вызывающие места,
    которые ещё не знают про optimistic, не ломались.
    """
    if optimistic_cache_enabled is None:
        optimistic_cache_enabled = True
    if stale_cache_ttl is None:
        stale_cache_ttl = 3600

    current = {
        "routed_cache_enabled": bool(routed_cache_enabled),
        "optimistic_cache_enabled": bool(optimistic_cache_enabled),
        "routed_cache_ttl": _as_int(routed_cache_ttl),
        "routed_reply_ttl": _as_int(routed_reply_ttl),
        "stale_cache_ttl": _as_int(stale_cache_ttl),
    }
    for name, values, _desc in ROUTED_PRESETS:
        match = True
        for f in _PRESET_FIELDS_BOOL:
            if current[f] != bool(values[f]):
                match = False
                break
        if not match:
            continue
        for f in _PRESET_FIELDS_INT:
            if current[f] != int(values[f]):
                match = False
                break
        if match:
            return name
    return "Пользовательский"


def get_routed_preset_map():
    return {name: values for name, values, _desc in ROUTED_PRESETS}
