"""
UmbraNet - UI-каталог сервисов для быстрых тумблеров в Маршрутизации.

Домены больше не хранятся здесь. Единый источник правды —
core/service_profiles.py. Этот модуль оставляет только UI-цвета категорий и
SERVICES-совместимые алиасы для существующей вкладки маршрутизации.
"""

from __future__ import annotations

from core.service_profiles import check_domains, preset_domains, services_in_category as _services_in_category, ui_services

# название категории -> (эмодзи, два цвета градиента иконки)
CATEGORIES = {
    "AI":      ("🤖", "#8b6dff", "#a855f7"),
    "Медиа":   ("🎬", "#4d8dff", "#22d3ee"),
    "Игры":    ("🎮", "#fb923c", "#ef4444"),
    "Работа":  ("💼", "#3ee089", "#10b981"),
    "Разное":  ("🧩", "#ff6478", "#ff6478"),
}

# сервис -> (категория, эмодзи, [домены])
# Оставляем имя SERVICES для обратной совместимости с routing.py.
SERVICES = ui_services()

# Все домены из пресетов (для отображения в ручном списке без дублирования метки)
PRESET_DOMAINS = preset_domains()

# Сервисы отсортированные по категориям для теста доступности
# (domain, display_name) — главный домен каждого сервиса
CHECK_DOMAINS = check_domains()


def services_in_category(cat: str) -> list[str]:
    return _services_in_category(cat)
