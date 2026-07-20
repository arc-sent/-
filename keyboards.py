"""Клавиатуры бота."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_BALANCE = "💰 Баланс"
BTN_SERVICES = "📋 Сервисы"
BTN_ORDER = "🛒 Создать заказ"
BTN_STATUS = "🔎 Статус заказа"
BTN_CANCEL = "✖️ Отмена"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_SERVICES)],
            [KeyboardButton(text=BTN_ORDER), KeyboardButton(text=BTN_STATUS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие…",
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def services_categories(groups: list[tuple[str, str, int]]) -> InlineKeyboardMarkup:
    """groups: список (key, label, count)."""
    rows = [
        [InlineKeyboardButton(text=f"{label} ({count})", callback_data=f"svc:cat:{key}")]
        for key, label, count in groups
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ К категориям", callback_data="svc:back")]]
    )


def order_categories(groups: list[tuple[str, str, int]]) -> InlineKeyboardMarkup:
    """Категории для выбора при создании заказа. groups: (key, label, count)."""
    rows = [
        [InlineKeyboardButton(text=f"{label} · {count}", callback_data=f"ord:cat:{key}")]
        for key, label, count in groups
        if count > 0
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_services(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Сервисы внутри категории. items: (service_id, button_label)."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"ord:svc:{sid}")]
        for sid, label in items
    ]
    rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="ord:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delivery_options(days_choices: tuple[int, ...]) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="⚡ Сразу", callback_data="drip:0")]
    for d in days_choices:
        row.append(InlineKeyboardButton(text=f"📆 {d} дн.", callback_data=f"drip:{d}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def confirm_order() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Оформить", callback_data="order:confirm"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="order:cancel"),
            ]
        ]
    )
