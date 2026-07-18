"""Курируемый каталог сервисов.

Здесь явно перечислены сервисы, которые показывает бот: ID -> (группа, русское
название как на сайте). Только эти сервисы попадают в меню и доступны для заказа.
Цена, лимиты (min/max) и тип берутся из API по этому ID.

Группы: "views" (просмотры), "likes" (лайки), "subs" (подписки) — см. grouping.py.

Чтобы добавить сервис: посмотри его ID в личном кабинете/через список сервисов и
допиши строку. Чтобы убрать — удали строку.
"""
from __future__ import annotations

from typing import Any

# service_id -> (group_key, "Русское название")
CATALOG: dict[int, tuple[str, str]] = {
    # 👀 Просмотры
    877: ("views", "Просмотры на Клипы Акция 🔥"),
    831: ("views", "Просмотры на Клипы Живые 👀"),
    # 👍 Лайки
    908: ("likes", "Лайки на Клипы Акция 🔥"),
    832: ("likes", "Лайки на Клипы Живые 👍"),
    # 👥 Подписки
    391: ("subs", "Подписчики Акция 🔥"),
    390: ("subs", "Подписчики Быстрые ⚡️"),
    484: ("subs", "Подписчики Оптима 👌"),
    1610: ("subs", "Подписчики Стандартные 👍"),
}


def entry(service_id: Any) -> tuple[str, str] | None:
    """Возвращает (группа, название) для сервиса или None, если его нет в каталоге."""
    try:
        return CATALOG.get(int(service_id))
    except (TypeError, ValueError):
        return None


def group_of(service_id: Any) -> str | None:
    e = entry(service_id)
    return e[0] if e else None


def name_of(service_id: Any, fallback: str = "") -> str:
    e = entry(service_id)
    return e[1] if e else fallback
