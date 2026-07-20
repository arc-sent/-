"""Капельная доставка (drip-feed): разбивка заказа на равные части во времени.

Цель — плавный, но «живой» график: total единиц распределяются частями по дням.
Внутри дня докрутки идут в дневное окно (например, ~9:00–23:00) со случайным
разбросом: границы окна и моменты докруток каждый день немного разные, интервалы
неровные — как у настоящей аудитории.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import config

DAY_SECONDS = 86400


def _cap_redistribute(counts: list[int], cap: int) -> None:
    """Срезает всё, что выше cap, и раскидывает излишек по порциям, где есть место."""
    overflow = 0
    for i, c in enumerate(counts):
        if c > cap:
            overflow += c - cap
            counts[i] = cap
    n = len(counts)
    i = 0
    guard = 0
    while overflow > 0 and guard < overflow + 4 * n + 16:
        room = cap - counts[i % n]
        if room > 0:
            add = min(room, overflow)
            counts[i % n] += add
            overflow -= add
        i += 1
        guard += 1


def _split_counts(
    total: int,
    days: int,
    service_min: int,
    runs_per_day: int | None,
    service_max: int | None = None,
) -> list[int]:
    """Размеры частей: РАЗНЫЕ («рваные»), не меньше service_min, сумма = total.

    Чтобы график выглядел живым, порции не равные, а случайного размера: у одних
    докруток объём заметно больше, у других меньше (напр. 455, 102, 577…). Каждая
    порция ≥ service_min и (если задан) ≤ service_max — лимит одного заказа в API.
    """
    total = int(total)
    min_chunk = max(1, int(service_min or 1))
    per_day = int(runs_per_day or config.DRIP_RUNS_PER_DAY)

    n = max(1, days * per_day)
    # Не больше, чем позволяет минимум порции.
    if total // n < min_chunk:
        n = max(1, total // min_chunk)
    # Если задан максимум — порций должно хватить, чтобы каждая влезла в лимит.
    cap = int(service_max) if service_max else 0
    if cap >= min_chunk and cap > 0:
        n = max(n, -(-total // cap))  # ceil(total / cap)
        n = min(n, max(1, total // min_chunk))

    if n <= 1:
        return [total]

    # Каждой порции гарантируем min_chunk, остаток раскидываем по случайным весам
    # с тяжёлым хвостом — отсюда заметный «рваный» разброс размеров.
    counts = [min_chunk] * n
    remaining = total - min_chunk * n
    if remaining > 0:
        weights = [random.expovariate(1.0) for _ in range(n)]
        wsum = sum(weights) or 1.0
        extra = [int(remaining * w / wsum) for w in weights]
        # Остаток от округления добираем по одной единице.
        for i in range(remaining - sum(extra)):
            extra[i % n] += 1
        counts = [c + e for c, e in zip(counts, extra)]

    if cap >= min_chunk and cap > 0:
        _cap_redistribute(counts, cap)

    random.shuffle(counts)
    return counts


def _per_day_counts(n: int, days: int) -> list[int]:
    """Сколько докруток в каждый из days дней (максимально ровно)."""
    base, rem = divmod(n, days)
    return [base + (1 if i < rem else 0) for i in range(days)]


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _uniform_offsets(n: int, days: int, jitter: int) -> list[int]:
    """Круглосуточный режим: равномерно + небольшой случайный сдвиг."""
    window = days * DAY_SECONDS
    step = window / n if n > 0 else 0
    offsets = []
    for i in range(n):
        j = random.uniform(-jitter, jitter) if i > 0 else 0
        offsets.append(max(0, int(round(i * step + j))))
    return sorted(offsets)


def _active_offsets(n: int, days: int, now_ts: int, jitter: int) -> list[int]:
    """Докрутки только в дневное окно, с «живым» разбросом границ и моментов."""
    start_h, end_h = config.DRIP_ACTIVE_HOURS
    tz = timezone(timedelta(hours=config.DRIP_TZ_OFFSET))
    now_local = datetime.fromtimestamp(now_ts, tz)

    counts = _per_day_counts(n, days)

    # Первый день: если сегодняшнее окно уже прошло — начинаем с завтра.
    base_mid = _midnight(now_local)
    if now_local.hour >= end_h:
        base_mid += timedelta(days=1)

    times: list[datetime] = []
    for d in range(days):
        c = counts[d]
        if c <= 0:
            continue
        day_mid = base_mid + timedelta(days=d)
        # Случайные границы окна на этот день.
        start = day_mid + timedelta(hours=start_h, seconds=random.uniform(-jitter, jitter))
        end = day_mid + timedelta(hours=end_h, seconds=random.uniform(-jitter, jitter))
        if end <= start:
            end = start + timedelta(hours=1)
        # В первый день не планируем в прошлое.
        if d == 0 and start < now_local:
            start = now_local
            if end <= start:
                end = start + timedelta(minutes=30)
        # Раскидываем c докруток по окну: слот на каждую + случайная точка внутри.
        span = (end - start).total_seconds()
        for j in range(c):
            slot_start = span * j / c
            point = slot_start + random.uniform(0.1, 0.9) * (span / c)
            times.append(start + timedelta(seconds=point))

    times.sort()
    return [max(0, int(t.timestamp() - now_ts)) for t in times]


def plan(
    total: int,
    days: int,
    service_min: int,
    now_ts: int,
    service_max: int | None = None,
) -> list[tuple[int, int]]:
    """Список (offset_seconds, quantity) — когда и сколько докрутить."""
    days = max(1, int(days))
    chunks = _split_counts(total, days, service_min, config.DRIP_RUNS_PER_DAY, service_max)
    n = len(chunks)
    jitter = max(0, int(config.DRIP_JITTER_MINUTES)) * 60
    if config.DRIP_ACTIVE_HOURS:
        offsets = _active_offsets(n, days, now_ts, jitter)
    else:
        offsets = _uniform_offsets(n, days, jitter)
    # На всякий случай выравниваем длины (offsets всегда == n).
    return list(zip(offsets, chunks))


def summarize(schedule: list[tuple[int, int]], days: int) -> str:
    """Короткое человекочитаемое описание плана."""
    n = len(schedule)
    if n <= 1:
        return "1 доставка"
    qtys = [q for _, q in schedule]
    per = f"рваными порциями от {min(qtys)} до {max(qtys)}"
    tail = ""
    if config.DRIP_ACTIVE_HOURS:
        s, e = config.DRIP_ACTIVE_HOURS
        tail = f", днём ~{s:02d}:00–{e:02d}:00 с живым разбросом"
    return f"{n} доставок {per} за {days} дн.{tail}"
