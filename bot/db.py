"""Хранилище на SQLite: пользователи и платежи."""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    source      TEXT,
    created_at  TEXT NOT NULL,
    file_id     TEXT,
    file_key    TEXT
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
    manual           INTEGER NOT NULL DEFAULT 0,
    is_test          INTEGER NOT NULL DEFAULT 0,
    delivery_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
"""

# Колонки, добавленные после первого релиза: старые базы на серверах дополняются при запуске
MIGRATIONS = {
    "payments": {
        "is_test": "INTEGER NOT NULL DEFAULT 0",
        "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
    },
    "users": {
        "file_id": "TEXT",
        "file_key": "TEXT",
    },
}


# Настоящая продажа: оплачена, не выдана вручную и не тестовый платёж
REAL_SALE = "status='succeeded' AND manual=0 AND is_test=0"


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
        # WAL: чтение не блокирует запись; busy_timeout — ждать, а не падать, если база занята
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {r["name"] for r in await cur.fetchall()}
            for name, ddl in columns.items():
                if name not in existing:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

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

    async def get_cached_file(self, user_id: int, key: str) -> str | None:
        """file_id уже отправленного пользователю экземпляра гайда (если сам файл не менялся)."""
        async with self.conn.execute("SELECT file_id, file_key FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        return row["file_id"] if row and row["file_id"] and row["file_key"] == key else None

    async def set_cached_file(self, user_id: int, key: str, file_id: str) -> None:
        await self.conn.execute("UPDATE users SET file_id=?, file_key=? WHERE user_id=?", (file_id, key, user_id))
        await self.conn.commit()

    # ---------- payments ----------
    async def next_inv_id(self) -> int:
        """Уникальный номер счёта для Робокассы (InvId).

        Номер не меньше текущего Unix-времени: если база потеряется или бота переустановят,
        новые счета не совпадут со старыми в Робокассе. Иначе проверка статуса нового счёта
        могла бы найти чужой старый оплаченный счёт с тем же номером и выдать гайд бесплатно."""
        floor = int(time.time())
        await self.conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('inv_id', ?)", (floor - 1,))
        await self.conn.execute("UPDATE counters SET value = MAX(value + 1, ?) WHERE name = 'inv_id'", (floor,))
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
                          confirmation_url: str | None, manual: bool = False, is_test: bool = False) -> None:
        await self.conn.execute(
            """INSERT INTO payments (payment_id, user_id, amount, status, confirmation_url, created_at, manual, is_test)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (payment_id, user_id, amount, status, confirmation_url, iso(now()), int(manual), int(is_test)),
        )
        await self.conn.commit()

    async def get_payment(self, payment_id: str):
        async with self.conn.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)) as cur:
            return await cur.fetchone()

    async def fresh_pending(self, user_id: int, ttl_min: int, amount: str, is_test: bool):
        """Неоплаченный счёт пользователя, созданный недавно на ту же сумму и в том же режиме, —
        чтобы не плодить ссылки. После смены цены или режима создаётся новый счёт."""
        since = iso(now() - timedelta(minutes=ttl_min))
        async with self.conn.execute(
            """SELECT * FROM payments WHERE user_id=? AND status='pending' AND created_at>=?
                 AND amount=? AND is_test=? AND manual=0
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, since, amount, int(is_test)),
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

    async def release_delivery(self, payment_id: str) -> int:
        """Выдача не удалась: снимаем отметку, чтобы повторить позже. Возвращает число неудачных попыток."""
        await self.conn.execute(
            "UPDATE payments SET delivered=0, delivery_attempts=delivery_attempts+1 WHERE payment_id=?",
            (payment_id,))
        await self.conn.commit()
        async with self.conn.execute("SELECT delivery_attempts FROM payments WHERE payment_id=?", (payment_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def undelivered_paid(self, max_attempts: int) -> list[str]:
        """Оплаченные, но не выданные счета (например, Telegram был недоступен) — для повторной выдачи."""
        async with self.conn.execute(
            """SELECT payment_id FROM payments
               WHERE status='succeeded' AND delivered=0 AND delivery_attempts<? ORDER BY paid_at""",
            (max_attempts,),
        ) as cur:
            return [r["payment_id"] for r in await cur.fetchall()]

    async def has_paid(self, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM payments WHERE user_id=? AND status='succeeded' LIMIT 1", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def stats(self) -> dict:
        q = {
            "users": "SELECT COUNT(*) FROM users",
            "buyers": f"SELECT COUNT(DISTINCT user_id) FROM payments WHERE {REAL_SALE}",
            "sales": f"SELECT COUNT(*) FROM payments WHERE {REAL_SALE}",
            "revenue": f"SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM payments WHERE {REAL_SALE}",
            "manual": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND manual=1",
            "test_sales": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND manual=0 AND is_test=1",
            "pending": "SELECT COUNT(*) FROM payments WHERE status='pending'",
            "undelivered": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND delivered=0",
            "today_sales": f"SELECT COUNT(*) FROM payments WHERE {REAL_SALE} AND paid_at>=date('now')",
            "today_users": "SELECT COUNT(*) FROM users WHERE created_at>=date('now')",
        }
        result = {}
        for key, sql in q.items():
            async with self.conn.execute(sql) as cur:
                result[key] = (await cur.fetchone())[0]
        async with self.conn.execute(
            """SELECT COALESCE(u.source,'—') AS src, COUNT(DISTINCT u.user_id) AS users,
                      COUNT(DISTINCT CASE WHEN p.status='succeeded' AND p.manual=0 AND p.is_test=0
                                          THEN p.user_id END) AS buyers
               FROM users u LEFT JOIN payments p ON p.user_id=u.user_id
               GROUP BY src ORDER BY users DESC LIMIT 15"""
        ) as cur:
            result["sources"] = [dict(r) for r in await cur.fetchall()]
        return result
