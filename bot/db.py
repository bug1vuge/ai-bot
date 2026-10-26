"""Хранилище на SQLite: пользователи и платежи."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id       TEXT PRIMARY KEY,
    user_id          INTEGER NOT NULL,
    amount           TEXT NOT NULL,
    status           TEXT NOT NULL,
    confirmation_url TEXT,
    created_at       TEXT NOT NULL,
    paid_at          TEXT,
    delivered        INTEGER NOT NULL DEFAULT 0,
    manual           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------- users ----------
    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None, source: str | None) -> None:
        # source (метка трафика) сохраняется только при первом визите
        await self.conn.execute(
            """INSERT INTO users (user_id, username, first_name, source, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (user_id, username, first_name, source, iso(now())),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int):
        async with self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone()

    # ---------- payments ----------
    async def next_inv_id(self, start: int = 1000) -> int:
        """Уникальный номер счёта для Робокассы (InvId), растёт с каждым платежом."""
        await self.conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('inv_id', ?)", (start,))
        await self.conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'inv_id'")
        async with self.conn.execute("SELECT value FROM counters WHERE name = 'inv_id'") as cur:
            value = (await cur.fetchone())[0]
        await self.conn.commit()
        return value

    async def ensure_inv_id_above(self, value: int) -> None:
        """После восстановления платежа счётчик не должен выдать уже использованный номер."""
        await self.conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('inv_id', ?)", (value,))
        await self.conn.execute("UPDATE counters SET value = MAX(value, ?) WHERE name = 'inv_id'", (value,))
        await self.conn.commit()

    async def add_payment(self, payment_id: str, user_id: int, amount: str, status: str,
                          confirmation_url: str | None, manual: bool = False) -> None:
        await self.conn.execute(
            """INSERT INTO payments (payment_id, user_id, amount, status, confirmation_url, created_at, manual)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (payment_id, user_id, amount, status, confirmation_url, iso(now()), int(manual)),
        )
        await self.conn.commit()

    async def get_payment(self, payment_id: str):
        async with self.conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)) as cur:
            return await cur.fetchone()

    async def fresh_pending(self, user_id: int, ttl_min: int):
        """Неоплаченный платёж пользователя, созданный недавно — чтобы не плодить ссылки."""
        since = iso(now() - timedelta(minutes=ttl_min))
        async with self.conn.execute(
            """SELECT * FROM payments WHERE user_id=? AND status='pending' AND created_at>=?
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, since),
        ) as cur:
            return await cur.fetchone()

    async def latest_pending(self, user_id: int):
        async with self.conn.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ) as cur:
            return await cur.fetchone()

    async def pending_for_polling(self, ttl_min: int) -> list[str]:
        since = iso(now() - timedelta(minutes=ttl_min))
        async with self.conn.execute(
            "SELECT payment_id FROM payments WHERE status='pending' AND created_at>=?", (since,)
        ) as cur:
            return [r["payment_id"] for r in await cur.fetchall()]

    async def set_status(self, payment_id: str, status: str) -> None:
        paid_at = iso(now()) if status == "succeeded" else None
        await self.conn.execute(
            "UPDATE payments SET status=?, paid_at=COALESCE(paid_at, ?) WHERE payment_id=?",
            (status, paid_at, payment_id),
        )
        await self.conn.commit()

    async def claim_delivery(self, payment_id: str) -> bool:
        """Атомарно «забирает» выдачу: True только для первого вызова. Защита от двойной выдачи."""
        cur = await self.conn.execute(
            "UPDATE payments SET delivered=1 WHERE payment_id=? AND status='succeeded' AND delivered=0",
            (payment_id,),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def release_delivery(self, payment_id: str) -> None:
        await self.conn.execute("UPDATE payments SET delivered=0 WHERE payment_id=?", (payment_id,))
        await self.conn.commit()

    async def has_paid(self, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM payments WHERE user_id=? AND status='succeeded' LIMIT 1", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def stats(self) -> dict:
        q = {
            "users": "SELECT COUNT(*) FROM users",
            "buyers": "SELECT COUNT(DISTINCT user_id) FROM payments WHERE status='succeeded' AND manual=0",
            "sales": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND manual=0",
            "revenue": "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM payments WHERE status='succeeded' AND manual=0",
            "manual": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND manual=1",
            "pending": "SELECT COUNT(*) FROM payments WHERE status='pending'",
            "today_sales": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND manual=0 AND paid_at>=date('now')",
            "today_users": "SELECT COUNT(*) FROM users WHERE created_at>=date('now')",
        }
        result = {}
        for key, sql in q.items():
            async with self.conn.execute(sql) as cur:
                result[key] = (await cur.fetchone())[0]
        async with self.conn.execute(
            """SELECT COALESCE(u.source,'—') AS src, COUNT(DISTINCT u.user_id) AS users,
                      COUNT(DISTINCT CASE WHEN p.status='succeeded' AND p.manual=0 THEN p.user_id END) AS buyers
               FROM users u LEFT JOIN payments p ON p.user_id=u.user_id
               GROUP BY src ORDER BY users DESC LIMIT 15"""
        ) as cur:
            result["sources"] = [dict(r) for r in await cur.fetchall()]
        return result
