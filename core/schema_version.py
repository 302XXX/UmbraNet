"""
Версии схем файлов состояния и миграции между ними (M1/M2).
===========================================================

Зачем. У программы два файла состояния: `config.json` (настройки) и
`umbranet_ui.json` (состояние интерфейса). Оба читались «как есть»: если поле
переименовали, убрали или поменяли его смысл, старый файл продолжал нести
старое значение — и оно молча побеждало новое умолчание. Пользователь видел,
что после обновления «настройка сама сбросилась» или, наоборот, «новая функция
не работает», хотя причина в одном: файл был из прошлой версии программы.

Как теперь. В каждом файле лежит номер версии схемы:
  • `config_version` — в `config.json`;
  • `state_version` — в `umbranet_ui.json`.
Файл без номера считается версией 0 («до появления версий»). При чтении версия
поднимается до текущей, применяя по одной миграции на каждый шаг. Миграция —
маленькая функция, которая правит словарь на месте и возвращает человеческое
описание того, что изменила (оно уходит в лог, чтобы у пользователя был след).
Так одноразовая правка настроек описывается один раз и не требует ручной
правки файла у каждого человека.

Важная оговорка про «файл из будущего». Если номер версии в файле БОЛЬШЕ
текущего (пользователь запустил старую сборку поверх новых настроек), миграции
не применяются и файл не перезаписывается: чужие поля этой программе неизвестны,
и перезапись означала бы их потерю. Вызывающий код получает признак `newer`
и решает сам (обычно — предупреждение в лог и работа на том, что понятно).
"""

from __future__ import annotations

import copy
import logging

log = logging.getLogger("UmbraNet.schema")


def read_version(raw: object, version_key: str, default: int = 0) -> int:
    """Номер версии из словаря: мусор и отрицательные значения — это `default`.

    Число может прийти строкой («1»): старые правки конфига руками так и делали,
    и считать это ошибкой незачем.
    """
    if not isinstance(raw, dict):
        return default
    value = raw.get(version_key)
    if isinstance(value, bool):          # bool — подкласс int, но версией не бывает
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return default
        return number if number >= 0 else default
    return default


def migrate(
    raw: object,
    *,
    version_key: str,
    target_version: int,
    migrations: dict[int, object] | None = None,
    logger: logging.Logger | None = None,
) -> tuple[dict, dict]:
    """Приводит словарь к текущей версии схемы.

    `migrations` — реестр «с версии N на N+1»: `{0: функция_0_в_1, 1: ...}`.
    Функция получает словарь, правит его на месте и возвращает описание
    изменения (строку) либо `None`, если менять было нечего. Отсутствие
    миграции для шага — не ошибка: значит на этом шаге менялась только версия.

    Возвращает `(cfg, report)`, где `report`:
      • `from_version` — версия, которая была в файле (0, если номера нет);
      • `to_version`   — версия на выходе;
      • `notes`        — описания применённых миграций;
      • `newer`        — True, если файл создан более новой версией программы;
      • `changed`      — True, если словарь отличается от исходного.
    """
    logger = logger or log
    registry = migrations or {}

    if not isinstance(raw, dict):
        return {}, {
            "from_version": 0,
            "to_version": target_version,
            "notes": [],
            "newer": False,
            "changed": False,
        }

    version = read_version(raw, version_key)
    if version > target_version:
        logger.warning(
            "Файл состояния создан более новой версией программы (%s=%s, эта версия знает %s): "
            "миграции не применяются, файл не перезаписывается",
            version_key, version, target_version,
        )
        return copy.deepcopy(raw), {
            "from_version": version,
            "to_version": version,
            "notes": [],
            "newer": True,
            "changed": False,
        }

    cfg = copy.deepcopy(raw)
    notes: list[str] = []
    while version < target_version:
        step = registry.get(version)
        if step is not None:
            note = step(cfg)
            if note:
                notes.append(note)
                logger.info("Миграция настроек (%s): %s", version_key, note)
        version += 1

    cfg[version_key] = target_version
    return cfg, {
        "from_version": read_version(raw, version_key),
        "to_version": target_version,
        "notes": notes,
        "newer": False,
        "changed": cfg != raw,
    }
