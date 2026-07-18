"""Группы сервисов: порядок и подписи для меню.

Принадлежность конкретного сервиса к группе задаётся в catalog.py.
"""
from __future__ import annotations

# Порядок и подписи групп. Ключи совпадают с группами в catalog.py.
GROUPS: list[tuple[str, str]] = [
    ("views", "👀 Просмотры"),
    ("likes", "👍 Лайки"),
    ("subs", "👥 Подписки"),
]

GROUP_LABELS: dict[str, str] = {key: label for key, label in GROUPS}
