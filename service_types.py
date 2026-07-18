"""Описание типов сервисов и требуемых для них полей при создании заказа.

Ключ — значение поля ``type`` из списка сервисов. Значение — список полей
(в порядке запроса у пользователя). Каждое поле: имя параметра API, подпись,
подсказка и тип ввода (для валидации/подсказки).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str            # имя параметра в API
    label: str           # подпись для пользователя
    hint: str            # что именно вводить
    kind: str = "text"   # text | int | multiline


LINK = Field("link", "Ссылка", "Пришли ссылку на пост/видео/профиль")
QUANTITY = Field("quantity", "Количество", "Пришли число (в пределах min–max)", "int")

# Типы сервисов -> поля. Имена типов взяты из документации (колонка «Тип»).
SERVICE_FIELDS: dict[str, list[Field]] = {
    "Default": [LINK, QUANTITY],
    "Custom Comments": [
        LINK,
        Field("comments", "Комментарии", "Пришли комментарии — каждый с новой строки", "multiline"),
    ],
    "Custom Comments Package": [
        LINK,
        Field("comments", "Комментарии", "Пришли комментарии — каждый с новой строки", "multiline"),
    ],
    "Comment Likes": [LINK, QUANTITY],
    "Poll": [
        LINK,
        QUANTITY,
        Field("answer_number", "Номер ответа", "Пришли номер варианта ответа", "int"),
    ],
    "Mentions Custom List": [
        LINK,
        Field("usernames", "Юзернеймы", "Пришли список юзернеймов — каждый с новой строки", "multiline"),
    ],
    "Mentions with Hashtags": [
        LINK,
        QUANTITY,
        Field("usernames", "Юзернеймы", "Пришли список юзернеймов — каждый с новой строки", "multiline"),
        Field("hashtags", "Хэштеги", "Пришли список хэштегов — каждый с новой строки", "multiline"),
    ],
    "Mentions Hashtag": [
        LINK,
        QUANTITY,
        Field("hashtag", "Хэштег", "Пришли один хэштег"),
    ],
    "Mentions User Followers": [
        LINK,
        QUANTITY,
        Field("username", "Юзернейм", "Пришли юзернейм, чьих подписчиков упомянуть"),
    ],
    "Username": [
        Field("username", "Юзернейм", "Пришли юзернейм"),
    ],
}

# Тип по умолчанию, если у сервиса встретился неизвестный type.
DEFAULT_FIELDS = [LINK, QUANTITY]


def fields_for(service_type: str) -> list[Field]:
    return SERVICE_FIELDS.get(service_type, DEFAULT_FIELDS)
