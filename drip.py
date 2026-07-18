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


def _split_counts(total: int, days: int, service_min: int, runs_per_day: int | None) -> list[int]:
    """Размеры частей: равные, не меньше service_min, сумма = total."""
    total = int(total)
    min_chunk = max(1, int(service_min or 1))
    per_day = int(runs_per_day or config.DRIP_RUNS_PER_DAY)

    n = max(1, days * per_day)
    if total // n < min_chunk:
        n = max(1, total // min_chunk)

    base = total // n
    rem = total - base * n
    return [base + (1 if i < rem else 0) for i in range(n)]


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


def plan(total: int, days: int, service_min: int, now_ts: int) -> list[tuple[int, int]]:
    """Список (offset_seconds, quantity) — когда и сколько докрутить."""
    days = max(1, int(days))
    chunks = _split_counts(total, days, service_min, config.DRIP_RUNS_PER_DAY)
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
    sizes = {q for _, q in schedule}
    per = f"по {next(iter(sizes))}" if len(sizes) == 1 else f"по ~{schedule[0][1]}"
    tail = ""
    if config.DRIP_ACTIVE_HOURS:
        s, e = config.DRIP_ACTIVE_HOURS
        tail = f", днём ~{s:02d}:00–{e:02d}:00 с живым разбросом"
    return f"{n} доставок {per} за {days} дн.{tail}"
