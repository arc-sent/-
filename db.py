"""Хранилище пользователей (SQLite): telegram_id -> API-ключ и настройки."""
from __future__ import annotations

import time
from typing import Any

import aiosqlite

import config

_db: aiosqlite.Connection | None = None


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            api_key     TEXT NOT NULL,
            currency    TEXT NOT NULL DEFAULT 'RUB',
            created_at  INTEGER NOT NULL
        )
        """
    )
    # Под-заказы капельной доставки (drip-feed).
    await _db.execute(
        """
        CREATE TABLE IF NOT EXISTS drip_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id     TEXT NOT NULL,
            telegram_id  INTEGER NOT NULL,
            service      INTEGER NOT NULL,
            link         TEXT NOT NULL DEFAULT '',
            params_json  TEXT NOT NULL,
            quantity     INTEGER NOT NULL,
            currency     TEXT NOT NULL,
            run_at       INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            api_order_id TEXT,
            error        TEXT,
            created_at   INTEGER NOT NULL
        )
        """
    )
    # Миграция старой БД: колонка link появилась позже.
    async with _db.execute("PRAGMA table_info(drip_runs)") as cur:
        cols = [r["name"] for r in await cur.fetchall()]
    if "link" not in cols:
        await _db.execute(
            "ALTER TABLE drip_runs ADD COLUMN link TEXT NOT NULL DEFAULT ''"
        )
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_drip_due ON drip_runs (status, run_at)"
    )
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_drip_link ON drip_runs (link, status)"
    )
    # Докрутки, зависшие в 'processing' после падения/рестарта, возвращаем в очередь:
    # при старте ничего реально «в полёте» нет.
    await _db.execute(
        "UPDATE drip_runs SET status = 'pending' WHERE status = 'processing'"
    )
    await _db.commit()


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("БД не инициализирована. Вызови db.init().")
    return _db


async def get_user(telegram_id: int) -> dict[str, Any] | None:
    async with _conn().execute(
        "SELECT telegram_id, api_key, currency, created_at FROM users WHERE telegram_id = ?",
        (telegram_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_api_key(telegram_id: int) -> str | None:
    user = await get_user(telegram_id)
    return user["api_key"] if user else None


async def upsert_user(telegram_id: int, api_key: str, currency: str) -> None:
    await _conn().execute(
        """
        INSERT INTO users (telegram_id, api_key, currency, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET api_key = excluded.api_key
        """,
        (telegram_id, api_key, currency, int(time.time())),
    )
    await _conn().commit()


async def set_currency(telegram_id: int, currency: str) -> None:
    await _conn().execute(
        "UPDATE users SET currency = ? WHERE telegram_id = ?",
        (currency, telegram_id),
    )
    await _conn().commit()


async def delete_user(telegram_id: int) -> None:
    await _conn().execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    await _conn().commit()


# ---------- Капельная доставка (drip-feed) ----------

async def add_drip_runs(runs: list[dict[str, Any]]) -> None:
    """runs: список словарей с ключами batch_id, telegram_id, service, link,
    params_json, quantity, currency, run_at."""
    now = int(time.time())
    await _conn().executemany(
        """
        INSERT INTO drip_runs
            (batch_id, telegram_id, service, link, params_json, quantity, currency, run_at, created_at)
        VALUES (:batch_id, :telegram_id, :service, :link, :params_json, :quantity, :currency, :run_at, :created_at)
        """,
        [{**r, "created_at": now} for r in runs],
    )
    await _conn().commit()


async def claim_due_drip_runs(now_ts: int, max_concurrent: int) -> list[dict[str, Any]]:
    """Забирает наступившие докрутки в работу, помечая их 'processing'.

    Правила:
    * не больше ``max_concurrent`` докруток в статусе 'processing' одновременно;
    * на один ролик (link) активна только одна партия (batch) — та, что стартовала
      раньше; докрутки более поздних партий на тот же ролик ждут её завершения.

    Пометка 'processing' атомарна (UPDATE ... WHERE status='pending'), поэтому одна
    и та же докрутка не будет отправлена дважды.
    """
    conn = _conn()

    # Сколько докруток уже «в полёте».
    async with conn.execute(
        "SELECT COUNT(*) AS c FROM drip_runs WHERE status = 'processing'"
    ) as cur:
        row = await cur.fetchone()
    slots = max_concurrent - (int(row["c"]) if row else 0)
    if slots <= 0:
        return []

    # Активная партия для каждого ролика = стартовавшая раньше всех среди незавершённых.
    async with conn.execute(
        """
        SELECT link, batch_id, MIN(run_at) AS start_at
        FROM drip_runs
        WHERE status IN ('pending', 'processing')
        GROUP BY link, batch_id
        """
    ) as cur:
        groups = [dict(r) for r in await cur.fetchall()]
    active_batch: dict[str, str] = {}
    active_start: dict[str, int] = {}
    for g in groups:
        link = g["link"]
        key = (g["start_at"], g["batch_id"])  # тай-брейк по batch_id для стабильности
        if link not in active_start or key < (active_start[link], active_batch[link]):
            active_start[link] = g["start_at"]
            active_batch[link] = g["batch_id"]

    # Наступившие докрутки, самые ранние первыми.
    async with conn.execute(
        """
        SELECT * FROM drip_runs
        WHERE status = 'pending' AND run_at <= ?
        ORDER BY run_at ASC
        """,
        (now_ts,),
    ) as cur:
        candidates = [dict(r) for r in await cur.fetchall()]

    claimed: list[dict[str, Any]] = []
    for run in candidates:
        if len(claimed) >= slots:
            break
        # Только докрутки активной партии этого ролика.
        if active_batch.get(run["link"]) != run["batch_id"]:
            continue
        upd = await conn.execute(
            "UPDATE drip_runs SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (run["id"],),
        )
        if upd.rowcount == 1:
            run["status"] = "processing"
            claimed.append(run)
    await conn.commit()
    return claimed


async def mark_drip_done(run_id: int, api_order_id: str) -> None:
    await _conn().execute(
        "UPDATE drip_runs SET status = 'done', api_order_id = ? WHERE id = ?",
        (str(api_order_id), run_id),
    )
    await _conn().commit()


async def mark_drip_failed(run_id: int, error: str) -> None:
    await _conn().execute(
        "UPDATE drip_runs SET status = 'failed', error = ? WHERE id = ?",
        (error[:500], run_id),
    )
    await _conn().commit()


async def drip_batch_stats(batch_id: str) -> dict[str, Any]:
    async with _conn().execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing,
            SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
            SUM(quantity) AS quantity
        FROM drip_runs WHERE batch_id = ?
        """,
        (batch_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else {}


async def active_drip_batches(telegram_id: int) -> list[dict[str, Any]]:
    async with _conn().execute(
        """
        SELECT batch_id, service, currency,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing,
               SUM(quantity) AS quantity,
               MIN(run_at) AS first_at, MAX(run_at) AS last_at
        FROM drip_runs
        WHERE telegram_id = ?
        GROUP BY batch_id
        HAVING (pending + processing) > 0
        ORDER BY last_at ASC
        """,
        (telegram_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
