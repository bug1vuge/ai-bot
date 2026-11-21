"""Хранилище на SQLite: пользователи и платежи.

Соединение работает в режиме автофиксации: каждый запрос сразу записывается на диск.
Операции из нескольких запросов выполняются в явной транзакции под общей блокировкой,
поэтому параллельные покупки не мешают друг другу.
"""
import asyncio
import contextlib
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

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
    payment_id        TEXT PRIMARY KEY,
    user_id           INTEGER NOT NULL,
    amount            TEXT NOT NULL,
    status            TEXT NOT NULL,
    confirmation_url  TEXT,
    created_at        TEXT NOT NULL,
    paid_at           TEXT,
    delivered         INTEGER NOT NULL DEFAULT 0,
    manual            INTEGER NOT NULL DEFAULT 0,
    is_test           INTEGER NOT NULL DEFAULT 0,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   TEXT,
    checked_at        TEXT,
    provider_id       TEXT,
    refund_state      TEXT,
    refund_attempts   INTEGER NOT NULL DEFAULT 0,
    note              TEXT
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
"""

# Индексы по колонкам, которые могли появиться только после миграции
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider_id);
"""

# Колонки, добавленные после первого релиза: старые базы на серверах дополняются при запуске
MIGRATIONS = {
    "payments": {
        "is_test": "INTEGER NOT NULL DEFAULT 0",
        "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",
        "checked_at": "TEXT",
        "provider_id": "TEXT",
        "refund_state": "TEXT",
        "refund_attempts": "INTEGER NOT NULL DEFAULT 0",
        "note": "TEXT",
    },
    "users": {
        "file_id": "TEXT",
        "file_key": "TEXT",
    },
}

# Статусы заказа (payments.status):
#   pending   — ждёт оплаты;            succeeded — оплачен (гайд выдаётся);
#   canceled  — не оплачен / отменён;    refunded  — оплачен, затем деньги возвращены;
#   duplicate — повторная оплата покупателя, у которого гайд уже есть: деньги возвращаются;
#   review    — платёж не прошёл проверку (сумма, магазин, метаданные) — разбирается администратор.
PENDING, SUCCEEDED, CANCELED, REFUNDED, DUPLICATE, REVIEW = (
    "pending", "succeeded", "canceled", "refunded", "duplicate", "review")

# Состояние выдачи (payments.delivered)
NOT_DELIVERED, DELIVERED, SENDING = 0, 1, 2

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
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if sqlite3.sqlite_version_info < (3, 35):
            raise RuntimeError(f"Нужен SQLite 3.35 или новее, установлен {sqlite3.sqlite_version}. "
                               "Используйте Ubuntu 22.04+, Debian 12+ или Python 3.12+ на Windows.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        # WAL: чтение не блокирует запись; busy_timeout — ждать, а не падать, если база занята
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        async with self.transaction():
            await self._migrate()
        await self.conn.executescript(INDEXES)
        async with self.transaction():
            # Выдача, прерванная перезапуском бота посреди отправки, будет повторена.
            # Лучше в редком случае прислать файл дважды, чем не прислать вовсе.
            cur = await self.conn.execute("UPDATE payments SET delivered=? WHERE delivered=?", (NOT_DELIVERED, SENDING))
            if cur.rowcount:
                log.warning("Незавершённых выдач после перезапуска: %s — будут повторены", cur.rowcount)

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
            self.conn = None

    @contextlib.asynccontextmanager
    async def transaction(self):
        """Атомарная операция из нескольких запросов. Запись в базу всегда идёт под одной блокировкой."""
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")

    async def _write(self, sql: str, params: tuple = ()) -> int:
        """Один изменяющий запрос. Возвращает число затронутых строк."""
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return cur.rowcount

    async def _one(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple = ()) -> list:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def ping(self) -> bool:
        try:
            return (await self._one("SELECT 1")) is not None
        except Exception:
            return False

    async def backup(self, target: Path) -> None:
        """Копия базы на ходу (безопасно при работающем боте)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        dst = await aiosqlite.connect(tmp)
        try:
            async with self._lock:
                await self.conn.backup(dst)
        finally:
            await dst.close()
        tmp.replace(target)

    # ---------- users ----------
    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None, source: str | None) -> None:
        # source (метка трафика) сохраняется только при первом визите
        await self._write(
            """INSERT INTO users (user_id, username, first_name, source, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (user_id, username, first_name, source, iso(now())),
        )

    async def get_user(self, user_id: int):
        return await self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def get_cached_file(self, user_id: int, key: str) -> str | None:
        """file_id уже отправленного пользователю экземпляра гайда (если сам файл не менялся)."""
        row = await self._one("SELECT file_id, file_key FROM users WHERE user_id=?", (user_id,))
        return row["file_id"] if row and row["file_id"] and row["file_key"] == key else None

    async def set_cached_file(self, user_id: int, key: str, file_id: str) -> None:
        await self._write("UPDATE users SET file_id=?, file_key=? WHERE user_id=?", (file_id, key, user_id))

    async def clear_cached_file(self, user_id: int) -> None:
        await self._write("UPDATE users SET file_id=NULL, file_key=NULL WHERE user_id=?", (user_id,))

    # ---------- payments ----------
    async def next_inv_id(self) -> int:
        """Уникальный номер заказа — одним атомарным запросом.

        Номер не меньше текущего Unix-времени: если база потеряется или бота переустановят,
        новые заказы не совпадут со старыми, и ключи идемпотентности ЮKassa (номер заказа)
        не повторятся."""
        floor = int(time.time())
        async with self._lock, self.conn.execute(
            """INSERT INTO counters (name, value) VALUES ('inv_id', ?)
                   ON CONFLICT(name) DO UPDATE SET value = MAX(value + 1, excluded.value)
                   RETURNING value""",
            (floor,),
        ) as cur:
            return (await cur.fetchone())[0]

    async def ensure_inv_id_above(self, value: int) -> None:
        """После восстановления заказа счётчик не должен выдать уже использованный номер."""
        await self._write(
            """INSERT INTO counters (name, value) VALUES ('inv_id', ?)
               ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)""",
            (value,),
        )

    async def add_payment(self, payment_id: str, user_id: int, amount: str, status: str,
                          confirmation_url: str | None, manual: bool = False, is_test: bool = False) -> bool:
        """Добавляет заказ. False — заказ с таким номером уже есть (повтор не создаёт дубликат)."""
        paid_at = iso(now()) if status == "succeeded" else None
        rows = await self._write(
            """INSERT OR IGNORE INTO payments
                 (payment_id, user_id, amount, status, confirmation_url, created_at, paid_at, manual, is_test)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payment_id, user_id, amount, status, confirmation_url, iso(now()), paid_at, int(manual), int(is_test)),
        )
        return rows == 1

    async def get_payment(self, payment_id: str):
        return await self._one("SELECT * FROM payments WHERE payment_id=?", (payment_id,))

    async def set_provider(self, payment_id: str, provider_id: str, url: str) -> None:
        """Заказ получил платёж в ЮKassa и ссылку на оплату."""
        await self._write("UPDATE payments SET provider_id=?, confirmation_url=? WHERE payment_id=?",
                          (provider_id, url, payment_id))

    async def by_provider(self, provider_id: str):
        return await self._one("SELECT * FROM payments WHERE provider_id=?", (provider_id,))

    async def open_order(self, user_id: int):
        """Неоплаченный заказ пользователя. Живой заказ у пользователя всегда один."""
        return await self._one(
            "SELECT * FROM payments WHERE user_id=? AND status=? AND manual=0 ORDER BY created_at DESC LIMIT 1",
            (user_id, PENDING))

    async def set_status(self, payment_id: str, status: str, *, only_from: tuple[str, ...] = (PENDING,),
                         note: str | None = None) -> bool:
        """Переход статуса только из разрешённых состояний: оплаченный заказ нельзя «отменить» и т.п."""
        marks = ",".join("?" * len(only_from))
        rows = await self._write(
            f"UPDATE payments SET status=?, note=COALESCE(?, note) WHERE payment_id=? AND status IN ({marks})",
            (status, note, payment_id, *only_from))
        return rows == 1

    async def settle_paid(self, payment_id: str) -> str:
        """Атомарно фиксирует успешную оплату заказа.

        Возвращает: paid — заказ стал оплаченным; already — уже был оплачен; duplicate — у покупателя
        уже есть другой оплаченный заказ (эта оплата лишняя и подлежит возврату); final — заказ в
        конечном статусе (возврат, проверка) и не меняется."""
        async with self.transaction() as conn:
            async with conn.execute("SELECT user_id, status FROM payments WHERE payment_id=?", (payment_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return "final"
            if row["status"] == SUCCEEDED:
                return "already"
            if row["status"] not in (PENDING, CANCELED):
                return "final"
            async with conn.execute(
                "SELECT 1 FROM payments WHERE user_id=? AND status=? AND payment_id<>? LIMIT 1",
                (row["user_id"], SUCCEEDED, payment_id),
            ) as cur:
                other = await cur.fetchone()
            new = DUPLICATE if other else SUCCEEDED
            await conn.execute("UPDATE payments SET status=?, paid_at=COALESCE(paid_at, ?) WHERE payment_id=?",
                               (new, iso(now()), payment_id))
            return "duplicate" if other else "paid"

    async def mark_paid(self, payment_id: str) -> None:
        """Заказ оплачен (ручная выдача администратором)."""
        await self._write(
            "UPDATE payments SET status='succeeded', paid_at=COALESCE(paid_at, ?) WHERE payment_id=?",
            (iso(now()), payment_id),
        )

    async def pending_for_polling(self, fresh_min: int, max_age_hours: int, slow_interval_min: int) -> list[str]:
        """Неоплаченные заказы с платежом в ЮKassa — для фоновой проверки статуса.
        Свежие проверяются каждый круг, старые (до max_age_hours) — не чаще раза в slow_interval_min:
        покупатель мог оплатить позже, а уведомление — потеряться."""
        t = now()
        return [r["payment_id"] for r in await self._all(
            """SELECT payment_id FROM payments
               WHERE status=? AND manual=0 AND provider_id IS NOT NULL AND created_at>=?
                 AND (created_at>=? OR checked_at IS NULL OR checked_at<?)
               ORDER BY created_at DESC""",
            (PENDING, iso(t - timedelta(hours=max_age_hours)), iso(t - timedelta(minutes=fresh_min)),
             iso(t - timedelta(minutes=slow_interval_min))))]

    async def mark_checked(self, payment_id: str) -> None:
        await self._write("UPDATE payments SET checked_at=? WHERE payment_id=?", (iso(now()), payment_id))

    async def expire_unsent(self, older_than_hours: int) -> int:
        """Заказы, для которых платёж в ЮKassa так и не создался, — закрываем."""
        return await self._write(
            "UPDATE payments SET status=?, note='платёж не создан' WHERE status=? AND provider_id IS NULL "
            "AND manual=0 AND created_at<?",
            (CANCELED, PENDING, iso(now() - timedelta(hours=older_than_hours))))

    async def expire_stale(self, older_than_hours: int) -> int:
        """Очень старые неоплаченные заказы больше не проверяются — закрываем."""
        return await self._write(
            "UPDATE payments SET status=?, note=COALESCE(note, 'истёк срок') WHERE status=? AND manual=0 AND created_at<?",
            (CANCELED, PENDING, iso(now() - timedelta(hours=older_than_hours))))

    async def set_refund_state(self, payment_id: str, state: str, *, attempt: bool = False) -> None:
        await self._write(
            "UPDATE payments SET refund_state=?, refund_attempts=refund_attempts+? WHERE payment_id=?",
            (state, int(attempt), payment_id))

    async def refunds_to_retry(self, max_attempts: int) -> list[str]:
        """Повторные оплаты, деньги по которым ещё не возвращены."""
        return [r["payment_id"] for r in await self._all(
            """SELECT payment_id FROM payments WHERE status=? AND provider_id IS NOT NULL
                 AND COALESCE(refund_state, '') NOT IN ('requested', 'succeeded', 'manual') AND refund_attempts<?""",
            (DUPLICATE, max_attempts))]

    async def claim_delivery(self, payment_id: str) -> bool:
        """Атомарно «забирает» выдачу: True только для одного вызова. Защита от двойной выдачи."""
        rows = await self._write(
            "UPDATE payments SET delivered=? WHERE payment_id=? AND status='succeeded' AND delivered=?",
            (SENDING, payment_id, NOT_DELIVERED),
        )
        return rows == 1

    async def finish_delivery(self, payment_id: str) -> None:
        await self._write("UPDATE payments SET delivered=?, next_attempt_at=NULL WHERE payment_id=?",
                          (DELIVERED, payment_id))

    async def release_delivery(self, payment_id: str, retry_in_sec: float) -> int:
        """Выдача не удалась: снимаем отметку и назначаем следующую попытку. Возвращает число неудач."""
        async with self._lock, self.conn.execute(
            """UPDATE payments SET delivered=?, delivery_attempts=delivery_attempts+1, next_attempt_at=?
                   WHERE payment_id=? RETURNING delivery_attempts""",
            (NOT_DELIVERED, iso(now() + timedelta(seconds=retry_in_sec)), payment_id),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def undelivered_paid(self, max_attempts: int, due_only: bool = True) -> list[str]:
        """Оплаченные, но не выданные заказы (например, Telegram был недоступен) — для повторной выдачи."""
        sql = """SELECT payment_id FROM payments
                 WHERE status='succeeded' AND delivered=? AND delivery_attempts<?"""
        params: tuple = (NOT_DELIVERED, max_attempts)
        if due_only:
            sql += " AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
            params += (iso(now()),)
        return [r["payment_id"] for r in await self._all(sql + " ORDER BY paid_at", params)]

    async def undelivered_for_user(self, user_id: int) -> list[str]:
        """Оплаченные, но ещё не выданные заказы пользователя (в том числе исчерпавшие попытки)."""
        return [r["payment_id"] for r in await self._all(
            "SELECT payment_id FROM payments WHERE user_id=? AND status='succeeded' AND delivered=? ORDER BY paid_at",
            (user_id, NOT_DELIVERED))]

    async def reset_attempts(self, payment_id: str) -> None:
        await self._write("UPDATE payments SET delivery_attempts=0, next_attempt_at=NULL WHERE payment_id=?",
                          (payment_id,))

    async def count_delivered(self, user_id: int, exclude: str) -> int:
        """Сколько других оплаченных и выданных заказов у пользователя — чтобы заметить двойную оплату."""
        row = await self._one(
            "SELECT COUNT(*) FROM payments WHERE user_id=? AND status='succeeded' AND delivered=? AND payment_id<>?",
            (user_id, DELIVERED, exclude))
        return row[0]

    async def has_status(self, user_id: int, status: str) -> bool:
        return await self._one(
            "SELECT 1 FROM payments WHERE user_id=? AND status=? LIMIT 1", (user_id, status)) is not None

    async def has_paid(self, user_id: int) -> bool:
        return await self._one(
            "SELECT 1 FROM payments WHERE user_id=? AND status='succeeded' LIMIT 1", (user_id,)) is not None

    async def stats(self) -> dict:
        q = {
            "users": "SELECT COUNT(*) FROM users",
            "buyers": f"SELECT COUNT(DISTINCT user_id) FROM payments WHERE {REAL_SALE}",
            "sales": f"SELECT COUNT(*) FROM payments WHERE {REAL_SALE}",
            "revenue": f"SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM payments WHERE {REAL_SALE}",
            "manual": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND manual=1",
            "test_sales": "SELECT COUNT(*) FROM payments WHERE status='succeeded' AND manual=0 AND is_test=1",
            "pending": "SELECT COUNT(*) FROM payments WHERE status='pending'",
            "refunded": "SELECT COUNT(*) FROM payments WHERE status='refunded'",
            "duplicates": "SELECT COUNT(*) FROM payments WHERE status='duplicate'",
            "review": "SELECT COUNT(*) FROM payments WHERE status='review'",
            "undelivered": f"SELECT COUNT(*) FROM payments WHERE status='succeeded' AND delivered<>{DELIVERED}",
            "today_sales": f"SELECT COUNT(*) FROM payments WHERE {REAL_SALE} AND paid_at>=date('now')",
            "today_users": "SELECT COUNT(*) FROM users WHERE created_at>=date('now')",
        }
        result = {key: (await self._one(sql))[0] for key, sql in q.items()}
        result["sources"] = [dict(r) for r in await self._all(
            """SELECT COALESCE(u.source,'—') AS src, COUNT(DISTINCT u.user_id) AS users,
                      COUNT(DISTINCT CASE WHEN p.status='succeeded' AND p.manual=0 AND p.is_test=0
                                          THEN p.user_id END) AS buyers
               FROM users u LEFT JOIN payments p ON p.user_id=u.user_id
               GROUP BY src ORDER BY users DESC LIMIT 15""")]
        return result

