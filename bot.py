"""Telegram-бот для API prmotion.me (сервисы VK).

Многопользовательский: каждый проходит регистрацию своим API-ключом, ключ
хранится в SQLite. Возможности: баланс, список VK-сервисов, создание заказов,
статусы.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from aiogram.utils.markdown import hbold

import catalog
import config
import db
import drip
import grouping as grp
import keyboards as kb
from api import ApiError, api
from service_types import Field, fields_for

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("prmotion-bot")

router = Router()

_services_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_CACHE_TTL = 300  # секунд


# ---------- FSM ----------

class RegStates(StatesGroup):
    waiting_key = State()


class OrderStates(StatesGroup):
    collecting = State()
    delivery = State()
    confirm = State()


class StatusStates(StatesGroup):
    waiting = State()


# ---------- Авторизация (middleware) ----------

# Обновления, разрешённые пользователю без сохранённого ключа (регистрация).
_REG_COMMANDS = {"/start", "/register", "/cancel"}


class AuthMiddleware(BaseMiddleware):
    """Проверяет доступ, подставляет api_key пользователя в data['api_key']."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # Глобальный allowlist (если задан ADMIN_IDS)
        if config.ADMIN_IDS and user.id not in config.ADMIN_IDS:
            await _reply(event, "⛔ Доступ запрещён.")
            return None

        api_key = await db.get_api_key(user.id)
        data["api_key"] = api_key
        if api_key is not None:
            return await handler(event, data)

        # Ключа нет — пропускаем только регистрацию
        if await _is_registration_update(event, data):
            return await handler(event, data)
        await _reply(event, "Ты ещё не зарегистрирован. Нажми /start, чтобы добавить API-ключ.")
        return None


async def _is_registration_update(event: TelegramObject, data: dict[str, Any]) -> bool:
    state: FSMContext | None = data.get("state")
    if state is not None and await state.get_state() == RegStates.waiting_key.state:
        return True
    if isinstance(event, Message) and event.text:
        return event.text.split()[0].lower() in _REG_COMMANDS
    return False


async def _reply(event: TelegramObject, text: str) -> None:
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)


# ---------- Утилиты ----------

async def get_services(api_key: str, force: bool = False) -> list[dict[str, Any]]:
    """Сервисы из курируемого каталога (catalog.py), с ценой/лимитами из API.

    Кэш общий.
    """
    now = time.time()
    if not force and _services_cache["data"] is not None and now - _services_cache["ts"] < _CACHE_TTL:
        return _services_cache["data"]
    data = await api.services(api_key)
    if not isinstance(data, list):
        raise ApiError("Неожиданный формат списка сервисов.")
    filtered = [s for s in data if catalog.entry(s.get("service")) is not None]
    _services_cache["data"] = filtered
    _services_cache["ts"] = now
    return filtered


def group_services(services: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Раскладывает сервисы по группам каталога (views/likes/subs)."""
    groups: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in grp.GROUPS}
    for s in services:
        key = catalog.group_of(s.get("service"))
        if key is not None:
            groups[key].append(s)
    return groups


def find_service(services: list[dict[str, Any]], service_id: str) -> dict[str, Any] | None:
    for s in services:
        if str(s.get("service")) == str(service_id):
            return s
    return None


def _fmt_amount(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


def unit_price(service: dict[str, Any]) -> float | None:
    """Цена за 1 штуку = rate / RATE_PER_UNITS."""
    try:
        return float(service.get("rate")) / config.RATE_PER_UNITS
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def fmt_service(s: dict[str, Any]) -> str:
    name = html.escape(catalog.name_of(s.get("service"), str(s.get("name", "—"))))
    price = unit_price(s)
    price_str = f"{_fmt_amount(price)} ₽/шт" if price is not None else f"{s.get('rate')}"
    return (
        f"🔹 <b>{s.get('service')}</b> — {name}\n"
        f"    💵 {price_str} · min {s.get('min')} / max {s.get('max')}"
    )


def _service_button_label(s: dict[str, Any]) -> str:
    """Короткая подпись кнопки: название + цена за штуку."""
    name = catalog.name_of(s.get("service"), str(s.get("name", "—")))
    price = unit_price(s)
    label = f"{name} · {_fmt_amount(price)}₽" if price is not None else name
    return label[:64]  # ограничение Telegram на текст кнопки


def fmt_status(order_id: str, st: dict[str, Any]) -> str:
    if "error" in st:
        return f"❌ Заказ <b>{html.escape(str(order_id))}</b>: {html.escape(str(st['error']))}"
    lines = [f"📦 Заказ <b>{html.escape(str(order_id))}</b>"]
    if st.get("status") is not None:
        lines.append(f"    Статус: {html.escape(str(st['status']))}")
    if st.get("start_count") is not None:
        lines.append(f"    Старт. счётчик: {html.escape(str(st['start_count']))}")
    if st.get("remains") is not None:
        lines.append(f"    Осталось: {html.escape(str(st['remains']))}")
    if st.get("charge") is not None:
        lines.append(f"    Списано: {html.escape(str(st['charge']))} {html.escape(str(st.get('currency', '')))}")
    return "\n".join(lines)


# ---------- Регистрация ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, api_key: str | None = None) -> None:
    await state.clear()
    if api_key:
        await message.answer(
            f"С возвращением, {hbold(message.from_user.first_name)}! 👋\n"
            "Выбирай действие в меню.",
            reply_markup=kb.main_menu(),
        )
        return
    await state.set_state(RegStates.waiting_key)
    await message.answer(
        f"Привет, {hbold(message.from_user.first_name)}! 👋\n"
        "Это панель управления VK-заказами prmotion.me.\n\n"
        "Для начала пришли свой <b>API-ключ</b> из личного кабинета prmotion.me.\n"
        "Он сохранится в боте, и ты сможешь работать со своим балансом и заказами.",
        reply_markup=kb.cancel_menu(),
    )


@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    await state.set_state(RegStates.waiting_key)
    await message.answer(
        "Пришли новый <b>API-ключ</b> — он заменит текущий.",
        reply_markup=kb.cancel_menu(),
    )


@router.message(RegStates.waiting_key)
async def reg_key(message: Message, state: FSMContext) -> None:
    key = (message.text or "").strip()
    if not key or " " in key or key.startswith("/"):
        await message.answer("Это не похоже на ключ. Пришли API-ключ одной строкой.")
        return
    # Проверяем ключ реальным запросом баланса
    try:
        data = await api.balance(key, config.DEFAULT_CURRENCY)
    except ApiError as exc:
        await message.answer(
            f"⚠️ Ключ не подошёл: {html.escape(str(exc))}\nПопробуй ещё раз или /cancel."
        )
        return
    await db.upsert_user(message.from_user.id, key, config.DEFAULT_CURRENCY)
    await state.clear()
    await message.answer(
        "✅ Готово, ключ сохранён!\n"
        f"💰 Текущий баланс: <b>{html.escape(str(data.get('balance')))}</b> "
        f"{html.escape(str(data.get('currency', '')))}",
        reply_markup=kb.main_menu(),
    )


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.delete_user(message.from_user.id)
    await message.answer(
        "Ключ удалён. Чтобы снова пользоваться ботом — /start.",
        reply_markup=kb.main_menu(),
    )


@router.message(Command("cancel"))
@router.message(F.text == kb.BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.", reply_markup=kb.main_menu())
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb.main_menu())


# ---------- Баланс ----------

@router.message(F.text == kb.BTN_BALANCE)
@router.message(Command("balance"))
async def show_balance(message: Message, api_key: str) -> None:
    try:
        data = await api.balance(api_key)
    except ApiError as exc:
        await message.answer(f"⚠️ Ошибка: {html.escape(str(exc))}")
        return
    await message.answer(
        f"💰 Баланс: <b>{html.escape(str(data.get('balance')))}</b> "
        f"{html.escape(str(data.get('currency', '')))}"
    )


# ---------- Сервисы ----------

def _categories_view(services: list[dict[str, Any]]) -> tuple[str, list[tuple[str, str, int]]]:
    grouped = group_services(services)
    rows = [(key, label, len(grouped[key])) for key, label in grp.GROUPS]
    text = (
        f"📋 Клип-сервисы VK — всего {len(services)}.\n"
        "Выбери категорию:"
    )
    return text, rows


def _group_view(services: list[dict[str, Any]], group_key: str) -> str:
    grouped = group_services(services)
    items = grouped.get(group_key, [])
    label = grp.GROUP_LABELS.get(group_key, group_key)
    if not items:
        return f"{label}\n\nВ этой категории пока нет сервисов."
    body = "\n\n".join(fmt_service(s) for s in items)
    return f"{label} — {len(items)} шт.\n\n{body}"


@router.message(F.text == kb.BTN_SERVICES)
@router.message(Command("services"))
async def show_services(message: Message, api_key: str) -> None:
    try:
        services = await get_services(api_key)
    except ApiError as exc:
        await message.answer(f"⚠️ Ошибка: {html.escape(str(exc))}")
        return
    if not services:
        await message.answer("Клип-сервисы VK не найдены.")
        return
    text, rows = _categories_view(services)
    await message.answer(text, reply_markup=kb.services_categories(rows))


@router.callback_query(F.data == "svc:back")
async def services_back(call: CallbackQuery, api_key: str) -> None:
    try:
        services = await get_services(api_key)
    except ApiError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    text, rows = _categories_view(services)
    await call.message.edit_text(text, reply_markup=kb.services_categories(rows))
    await call.answer()


@router.callback_query(F.data.startswith("svc:cat:"))
async def services_category(call: CallbackQuery, api_key: str) -> None:
    group_key = call.data.split(":", 2)[2]
    try:
        services = await get_services(api_key)
    except ApiError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.message.edit_text(_group_view(services, group_key), reply_markup=kb.services_back())
    await call.answer()


# ---------- Создание заказа ----------

_ORDER_CATEGORIES_TEXT = "🛒 <b>Создание заказа</b>\nВыбери категорию:"


@router.message(F.text == kb.BTN_ORDER)
@router.message(Command("order"))
async def order_start(message: Message, state: FSMContext, api_key: str) -> None:
    await state.clear()
    try:
        services = await get_services(api_key)
    except ApiError as exc:
        await message.answer(f"⚠️ Ошибка: {html.escape(str(exc))}")
        return
    if not services:
        await message.answer("Сервисы сейчас недоступны. Попробуй позже.")
        return
    _, rows = _categories_view(services)
    await message.answer(_ORDER_CATEGORIES_TEXT, reply_markup=kb.order_categories(rows))


@router.callback_query(F.data == "ord:back")
async def order_pick_back(call: CallbackQuery, api_key: str) -> None:
    try:
        services = await get_services(api_key)
    except ApiError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    _, rows = _categories_view(services)
    await call.message.edit_text(_ORDER_CATEGORIES_TEXT, reply_markup=kb.order_categories(rows))
    await call.answer()


@router.callback_query(F.data.startswith("ord:cat:"))
async def order_pick_category(call: CallbackQuery, api_key: str) -> None:
    group_key = call.data.split(":", 2)[2]
    try:
        services = await get_services(api_key)
    except ApiError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    items = group_services(services).get(group_key, [])
    label = grp.GROUP_LABELS.get(group_key, group_key)
    if not items:
        await call.answer("В этой категории пока нет сервисов.", show_alert=True)
        return
    btns = [(str(s.get("service")), _service_button_label(s)) for s in items]
    await call.message.edit_text(
        f"{label}\nВыбери сервис:",
        reply_markup=kb.order_services(btns),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ord:svc:"))
async def order_pick_service(call: CallbackQuery, state: FSMContext, api_key: str) -> None:
    service_id = call.data.split(":", 2)[2]
    try:
        services = await get_services(api_key)
    except ApiError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    service = find_service(services, service_id)
    if service is None:
        await call.answer("Сервис не найден — обнови список и попробуй снова.", show_alert=True)
        return

    fields = fields_for(str(service.get("type", "Default")))
    await state.set_state(OrderStates.collecting)
    await state.update_data(
        service=service,
        params={},
        field_index=0,
        currency=config.DEFAULT_CURRENCY,
    )
    await call.message.edit_text(f"✅ Выбран сервис:\n{fmt_service(service)}")
    await call.answer()
    await _ask_next_field(call.message, state, fields, 0, with_cancel=True)


async def _ask_next_field(
    message: Message,
    state: FSMContext,
    fields: list[Field],
    index: int,
    with_cancel: bool = False,
) -> None:
    if index >= len(fields):
        await _finish_collecting(message, state)
        return
    field = fields[index]
    await state.set_state(OrderStates.collecting)
    await message.answer(
        f"✏️ <b>{field.label}</b>\n{field.hint}",
        reply_markup=kb.cancel_menu() if with_cancel else None,
    )


def _is_drippable(params: dict[str, Any]) -> bool:
    """Капельная доставка возможна для сервисов с числовым quantity."""
    return str(params.get("quantity", "")).isdigit()


async def _finish_collecting(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    params = data["params"]
    service = data["service"]
    if _is_drippable(params):
        group = catalog.group_of(service.get("service"))
        choices = config.drip_days_for(group)
        period = "сутки" if choices == (1,) else "несколько дней"
        await state.set_state(OrderStates.delivery)
        await message.answer(
            "🚚 <b>Как доставить?</b>\n"
            "⚡ <b>Сразу</b> — весь объём одним заказом.\n"
            f"📆 <b>Капельно</b> — раздробим на равные части и будем докручивать "
            f"с живым разбросом ({period}), чтобы график рос плавно и «живо».",
            reply_markup=kb.delivery_options(choices),
        )
        return
    await state.update_data(drip_days=0)
    await _show_summary(message, state)


@router.callback_query(OrderStates.delivery, F.data.startswith("drip:"))
async def order_delivery(call: CallbackQuery, state: FSMContext) -> None:
    days = int(call.data.split(":", 1)[1])
    await state.update_data(drip_days=days)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await _show_summary(call.message, state)


def _validate_field(field: Field, value: str, service: dict[str, Any]) -> tuple[bool, str]:
    value = value.strip()
    if not value:
        return False, "Пустое значение. Попробуй ещё раз."
    if field.kind == "int":
        if not value.isdigit():
            return False, "Нужно число. Попробуй ещё раз."
        if field.name == "quantity":
            qty = int(value)
            mn, mx = service.get("min"), service.get("max")
            try:
                if mn is not None and qty < int(mn):
                    return False, f"Минимум для этого сервиса — {mn}."
                if mx is not None and qty > int(mx):
                    return False, f"Максимум для этого сервиса — {mx}."
            except (TypeError, ValueError):
                pass
    return True, value


@router.message(OrderStates.collecting)
async def order_collect(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    service = data["service"]
    params: dict[str, Any] = data["params"]
    index: int = data["field_index"]
    fields = fields_for(str(service.get("type", "Default")))
    field = fields[index]

    ok, result = _validate_field(field, message.text or "", service)
    if not ok:
        await message.answer(f"⚠️ {result}")
        return

    params[field.name] = result
    index += 1
    await state.update_data(params=params, field_index=index)
    await _ask_next_field(message, state, fields, index)


def _order_quantity(params: dict[str, Any]) -> int | None:
    """Количество единиц заказа для расчёта суммы (из quantity или длины списка)."""
    qty = params.get("quantity")
    if qty is not None and str(qty).isdigit():
        return int(qty)
    for key in ("comments", "usernames"):
        if params.get(key):
            lines = [ln for ln in re.split(r"\r?\n", str(params[key])) if ln.strip()]
            return len(lines)
    return None


def _estimate_cost(service: dict[str, Any], params: dict[str, Any]) -> tuple[int, float] | None:
    """(количество, сумма) = цена_за_штуку * количество. None, если не посчитать."""
    qty = _order_quantity(params)
    if qty is None:
        return None
    price = unit_price(service)
    if price is None:
        return None
    return qty, price * qty


def _build_schedule(service: dict[str, Any], params: dict[str, Any], drip_days: int) -> list[tuple[int, int]]:
    """Расписание докруток (offset_seconds, quantity). Пусто, если drip не применим."""
    if drip_days <= 0 or not _is_drippable(params):
        return []
    total = int(params["quantity"])
    try:
        service_min = int(service.get("min"))
    except (TypeError, ValueError):
        service_min = 1
    try:
        service_max = int(service.get("max"))
    except (TypeError, ValueError):
        service_max = None
    return drip.plan(total, drip_days, service_min, int(time.time()), service_max)


async def _show_summary(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    service = data["service"]
    params = data["params"]
    currency = data["currency"]
    drip_days = int(data.get("drip_days", 0))

    lines = [
        "🧾 <b>Проверь заказ:</b>",
        f"Сервис: {service.get('service')} — {html.escape(catalog.name_of(service.get('service'), str(service.get('name'))))}",
        f"Валюта: {currency}",
    ]
    for k, v in params.items():
        shown = html.escape(str(v))
        if len(shown) > 300:
            shown = shown[:300] + "…"
        lines.append(f"{k}: <code>{shown}</code>")

    estimate = _estimate_cost(service, params)
    price = unit_price(service)
    if estimate is not None:
        qty, total = estimate
        lines.append(
            f"💵 Цена: {_fmt_amount(price)} ₽/шт × {qty} = "
            f"<b>≈ {_fmt_amount(total)} {currency}</b>"
        )
    elif price is not None:
        lines.append(f"💵 Цена: {_fmt_amount(price)} ₽/шт")

    schedule = _build_schedule(service, params, drip_days)
    if schedule:
        lines.append(f"🚚 Доставка: капельно — {drip.summarize(schedule, drip_days)}")
    else:
        lines.append("🚚 Доставка: сразу")

    await state.set_state(OrderStates.confirm)
    await message.answer("\n".join(lines), reply_markup=kb.confirm_order())


@router.callback_query(OrderStates.confirm, F.data == "order:cancel")
async def order_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Заказ отменён.")
    await call.message.answer("Меню:", reply_markup=kb.main_menu())
    await call.answer()


@router.callback_query(OrderStates.confirm, F.data == "order:confirm")
async def order_confirm(call: CallbackQuery, state: FSMContext, api_key: str) -> None:
    data = await state.get_data()
    service = data["service"]
    params = data["params"]
    currency = data["currency"]
    drip_days = int(data.get("drip_days", 0))

    schedule = _build_schedule(service, params, drip_days)
    if len(schedule) > 1:
        await _create_drip(call, state, service, params, currency, schedule, drip_days)
        return

    request = {"service": service.get("service"), "currency": currency, **params}
    await call.answer("Отправляю…")
    try:
        result = await api.add_order(api_key, request)
    except ApiError as exc:
        await call.message.edit_text(f"⚠️ Ошибка создания заказа: {html.escape(str(exc))}")
        await state.clear()
        await call.message.answer("Меню:", reply_markup=kb.main_menu())
        return

    order_id = result.get("order")
    await state.clear()
    await call.message.edit_text(
        f"✅ Заказ создан! Номер заказа: <b>{html.escape(str(order_id))}</b>\n"
        f"Проверить статус: /status {order_id}"
    )
    await call.message.answer("Меню:", reply_markup=kb.main_menu())


async def _create_drip(
    call: CallbackQuery,
    state: FSMContext,
    service: dict[str, Any],
    params: dict[str, Any],
    currency: str,
    schedule: list[tuple[int, int]],
    drip_days: int,
) -> None:
    batch_id = uuid.uuid4().hex[:12]
    # Ключ ролика для сериализации. Если ссылки нет — привязываемся к самой партии,
    # чтобы разные заказы без ссылки не блокировали друг друга.
    link = str(params.get("link") or params.get("username") or "").strip() or batch_id
    now = int(time.time())
    runs = []
    for offset, chunk in schedule:
        run_params = {**params, "quantity": str(chunk)}
        runs.append({
            "batch_id": batch_id,
            "telegram_id": call.from_user.id,
            "service": int(service.get("service")),
            "link": link,
            "params_json": json.dumps(run_params, ensure_ascii=False),
            "quantity": int(chunk),
            "currency": currency,
            "run_at": now + offset,
        })
    await db.add_drip_runs(runs)
    await state.clear()
    await call.answer("Запланировано!")

    total = int(params["quantity"])
    last_h = round(schedule[-1][0] / 3600, 1)
    await call.message.edit_text(
        "✅ <b>Капельная доставка запущена!</b>\n"
        f"Сервис: {service.get('service')} — "
        f"{html.escape(catalog.name_of(service.get('service'), str(service.get('name'))))}\n"
        f"Всего: <b>{total}</b> · {drip.summarize(schedule, drip_days)}\n"
        f"Первая докрутка — в ближайшую минуту, последняя — через ~{last_h} ч.\n"
        "Я пришлю сообщение, когда всё будет отправлено. Прогресс: /drips"
    )
    await call.message.answer("Меню:", reply_markup=kb.main_menu())


# ---------- Статус ----------

@router.message(F.text == kb.BTN_STATUS)
async def status_prompt(message: Message, state: FSMContext) -> None:
    await state.set_state(StatusStates.waiting)
    await message.answer(
        "🔎 Пришли <b>ID заказа</b> (или несколько через запятую).",
        reply_markup=kb.cancel_menu(),
    )


@router.message(Command("status"))
async def status_command(message: Message, state: FSMContext, command: CommandObject, api_key: str) -> None:
    args = (command.args or "").strip()
    if not args:
        await status_prompt(message, state)
        return
    await _do_status(message, args, api_key)


@router.message(StatusStates.waiting)
async def status_input(message: Message, state: FSMContext, api_key: str) -> None:
    await state.clear()
    await _do_status(message, message.text or "", api_key)
    await message.answer("Меню:", reply_markup=kb.main_menu())


async def _do_status(message: Message, raw: str, api_key: str) -> None:
    ids = [x.strip() for x in raw.replace(" ", ",").split(",") if x.strip()]
    if not ids:
        await message.answer("Не вижу ID заказа.")
        return
    invalid = [x for x in ids if not x.isdigit()]
    if invalid:
        await message.answer(f"Некорректные ID: {', '.join(map(html.escape, invalid))}")
        return
    try:
        if len(ids) == 1:
            st = await api.status(api_key, ids[0])
            await message.answer(fmt_status(ids[0], st))
        else:
            data = await api.multi_status(api_key, ids)
            blocks = [fmt_status(oid, st) for oid, st in data.items()]
            await message.answer("\n\n".join(blocks))
    except ApiError as exc:
        await message.answer(f"⚠️ Ошибка: {html.escape(str(exc))}")


# ---------- Капельная доставка: список и планировщик ----------

@router.message(Command("drips"))
async def cmd_drips(message: Message) -> None:
    batches = await db.active_drip_batches(message.from_user.id)
    if not batches:
        await message.answer("Активных капельных доставок нет.")
        return
    now = int(time.time())
    lines = ["💧 <b>Активные капельные доставки:</b>"]
    for b in batches:
        done = b["total"] - (b["pending"] or 0) - (b.get("processing") or 0)
        left_h = max(0, round((b["last_at"] - now) / 3600, 1))
        name = catalog.name_of(b["service"], f"сервис {b['service']}")
        lines.append(
            f"\n• {html.escape(name)}\n"
            f"   Отправлено {done}/{b['total']} · осталось ~{left_h} ч · всего {b['quantity']}"
        )
    await message.answer("\n".join(lines))


async def _process_drip_run(bot: Bot, run: dict[str, Any]) -> None:
    """Отправляет один под-заказ и обновляет его статус."""
    run_id = run["id"]
    tg_id = run["telegram_id"]
    api_key = await db.get_api_key(tg_id)
    if not api_key:
        await db.mark_drip_failed(run_id, "нет API-ключа (пользователь вышел)")
        return
    try:
        params = json.loads(run["params_json"])
    except json.JSONDecodeError as exc:
        await db.mark_drip_failed(run_id, f"битые параметры: {exc}")
        return

    request = {"service": run["service"], "currency": run["currency"], **params}
    try:
        result = await api.add_order(api_key, request)
        await db.mark_drip_done(run_id, result.get("order"))
    except ApiError as exc:
        await db.mark_drip_failed(run_id, str(exc))
        try:
            await bot.send_message(
                tg_id,
                f"⚠️ Капельная докрутка не прошла (сервис {run['service']}, "
                f"{run['quantity']} шт): {html.escape(str(exc))}",
            )
        except Exception:  # noqa: BLE001 — не роняем планировщик из-за уведомления
            logger.exception("Не удалось уведомить о сбое докрутки")

    # Если это была последняя докрутка партии — отчитаемся.
    stats = await db.drip_batch_stats(run["batch_id"])
    if stats and (stats.get("pending", 0) or 0) == 0 and (stats.get("processing", 0) or 0) == 0:
        done = stats.get("done", 0)
        failed = stats.get("failed", 0)
        text = (
            "💧 <b>Капельная доставка завершена</b>\n"
            f"Успешно докручено заказов: {done}"
        )
        if failed:
            text += f"\n❌ Не прошло: {failed}"
        try:
            await bot.send_message(tg_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить отчёт о завершении")


async def drip_scheduler(bot: Bot) -> None:
    """Фоновый цикл: отправляет наступившие докрутки."""
    logger.info("Планировщик капельной доставки запущен")
    while True:
        try:
            claimed = await db.claim_due_drip_runs(
                int(time.time()), config.DRIP_MAX_CONCURRENT
            )
            if claimed:
                # Захваченные докрутки уже помечены 'processing' и их не больше лимита —
                # отправляем параллельно. return_exceptions, чтобы один сбой не отменял остальные.
                await asyncio.gather(
                    *(_process_drip_run(bot, run) for run in claimed),
                    return_exceptions=True,
                )
        except Exception:  # noqa: BLE001 — цикл не должен падать
            logger.exception("Ошибка в планировщике капельной доставки")
        await asyncio.sleep(config.DRIP_TICK_SECONDS)


# ---------- Запуск ----------

async def main() -> None:
    await db.init()
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.include_router(router)
    scheduler_task = asyncio.create_task(drip_scheduler(bot))
    try:
        me = await bot.get_me()
        logger.info("Бот запущен: @%s", me.username)
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        await api.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка бота.")
