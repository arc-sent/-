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
    await _db.execute(
        "CREATE INDEX IF NOT EXISTS idx_drip_due ON drip_runs (status, run_at)"
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
    """runs: список словарей с ключами batch_id, telegram_id, service, params_json,
    quantity, currency, run_at."""
    now = int(time.time())
    await _conn().executemany(
        """
        INSERT INTO drip_runs
            (batch_id, telegram_id, service, params_json, quantity, currency, run_at, created_at)
        VALUES (:batch_id, :telegram_id, :service, :params_json, :quantity, :currency, :run_at, :created_at)
        """,
        [{**r, "created_at": now} for r in runs],
    )
    await _conn().commit()


async def due_drip_runs(now_ts: int, limit: int = 50) -> list[dict[str, Any]]:
    async with _conn().execute(
        """
        SELECT * FROM drip_runs
        WHERE status = 'pending' AND run_at <= ?
        ORDER BY run_at ASC
        LIMIT ?
        """,
        (now_ts, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


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
               SUM(quantity) AS quantity,
               MIN(run_at) AS first_at, MAX(run_at) AS last_at
        FROM drip_runs
        WHERE telegram_id = ?
        GROUP BY batch_id
        HAVING pending > 0
        ORDER BY last_at ASC
        """,
        (telegram_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
