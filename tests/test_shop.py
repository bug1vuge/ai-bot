"""Тесты оплаты через Prodamus и выдачи гайда — без сети и реальных денег."""
import asyncio
import io
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfReader

import bot.db as dbmod
from bot.config import Config
from bot.db import SENDING, Database
from bot.prodamus import Prodamus, sign
from bot.service import MAX_DELIVERY_ATTEMPTS, Shop, _same_amount
from bot.web import make_app

USER = 111
SECRET = "test-secret-key"
FORM = "https://aibezlesa.payform.ru"


class FakeBot:
    def __init__(self, fail_documents=0):
        self.messages, self.documents = [], []
        self.fail_documents = fail_documents

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, caption=None, **kw):
        if self.fail_documents:
            self.fail_documents -= 1
            raise RuntimeError("telegram down")
        self.documents.append((chat_id, document))
        return SimpleNamespace(document=SimpleNamespace(file_id="FILEID"))


def make_cfg(tmp_path, product_file):
    cfg = Config()
    cfg.pay_url, cfg.pay_secret, cfg.test_mode = FORM, SECRET, False
    cfg.public_url = "https://bot.example.ru"
    cfg.db_path = tmp_path / "t.db"
    cfg.product_file = product_file
    cfg.admin_ids = [999]
    cfg.price_rub = 890
    cfg.watermark = True
    return cfg


@pytest.fixture
async def shop(tmp_path, sample_pdf):
    cfg = make_cfg(tmp_path, sample_pdf)
    db = Database(cfg.db_path)
    await db.connect()
    await db.upsert_user(USER, "ivan", "Иван", "tiktok")
    s = Shop(cfg, db, Prodamus(cfg.pay_url, cfg.pay_secret), FakeBot())
    s.bot_username = "guide_bot"
    yield s
    await db.close()


def notification(order, total="890.00", status="success"):
    """Уведомление Prodamus в том виде, в каком его разбирает PHP ($_POST)."""
    return {
        "date": "2026-10-26T22:40:00+03:00", "order_id": "31337", "order_num": order, "domain": "aibezlesa.payform.ru",
        "sum": total, "customer_phone": "+79991234567", "customer_email": "a@b.ru", "customer_extra": "Telegram ID 111",
        "payment_type": "Оплата картой", "commission": "3.5", "commission_sum": "31.15", "attempt": "1",
        "products": [{"name": "Гайд ИИ без тёмного леса, PDF", "price": total, "quantity": "1", "sum": total}],
        "payment_status": status, "payment_status_description": "Успешная оплата", "payment_init": "manual",
    }


def form_body(data: dict) -> str:
    """Кодирует вложенные данные как PHP http_build_query: products[0][name]=…"""
    flat = []

    def walk(prefix, value):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}[{k}]" if prefix else k, v)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(f"{prefix}[{i}]", v)
        else:
            flat.append((prefix, value))
    walk("", data)
    return urlencode(flat)


async def paid(shop, data):
    """Уведомление Prodamus + ожидание фоновой выдачи гайда."""
    ok = await shop.handle_notification(data)
    await shop.wait_background()
    return ok


def admin_texts(shop):
    return [t for chat, t in shop.bot.messages if chat == 999]


# ---------- ссылка на оплату ----------
async def test_payment_link(shop):
    pid, url = await shop.get_or_create_payment(USER)
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith(FORM + "/?")
    assert q["do"] == "pay" and q["order_id"] == pid and pid.endswith(f"-{USER}")
    assert q["products[0][price]"] == "890.00" and q["products[0][quantity]"] == "1"
    assert q["urlSuccess"] == "https://bot.example.ru/pay/success"
    assert q["urlReturn"] == "https://bot.example.ru/pay/return"
    assert q["npd_income_type"] == "FROM_INDIVIDUAL"


async def test_create_reuses_link_and_unique_ids(shop):
    pid1, url1 = await shop.get_or_create_payment(USER)
    pid2, url2 = await shop.get_or_create_payment(USER)
    assert pid1 == pid2 and url1 == url2
    pid3, _ = await shop.get_or_create_payment(222)
    assert int(pid3.split("-")[0]) == int(pid1.split("-")[0]) + 1


async def test_price_or_mode_change_creates_new_order(shop):
    pid1, _ = await shop.get_or_create_payment(USER)
    shop.cfg.price_rub = 990
    pid2, url2 = await shop.get_or_create_payment(USER)
    assert pid1 != pid2 and "990.00" in url2
    shop.cfg.test_mode = True
    pid3, _ = await shop.get_or_create_payment(USER)
    assert pid3 != pid2


async def test_double_click_creates_one_order(shop):
    results = await asyncio.gather(*[shop.get_or_create_payment(USER) for _ in range(5)])
    assert len({pid for pid, _ in results}) == 1
    assert shop._locks == {}


async def test_many_buyers_at_once_get_unique_orders(shop):
    for uid in range(1, 51):
        await shop.db.upsert_user(uid, None, "U", None)
    results = await asyncio.gather(*[shop.get_or_create_payment(uid) for uid in range(1, 51)])
    assert len({pid for pid, _ in results}) == 50


async def test_order_ids_survive_database_loss(shop, tmp_path, monkeypatch):
    pid, _ = await shop.get_or_create_payment(USER)
    later = time.time() + 60
    monkeypatch.setattr("bot.db.time.time", lambda: later)
    fresh = Database(tmp_path / "new.db")
    await fresh.connect()
    try:
        assert await fresh.next_inv_id() > int(pid.split("-")[0])
    finally:
        await fresh.close()


# ---------- уведомление об оплате ----------
async def test_notification_delivers_exactly_once(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    data = notification(pid)
    results = await asyncio.gather(*[shop.handle_notification(data) for _ in range(5)],
                                   *[shop.process_payment(pid) for _ in range(3)])
    await shop.wait_background()
    assert all(r in (True, "succeeded", "pending", "already_delivered", "in_progress") for r in results)
    assert len(shop.bot.documents) == 1
    assert await shop.db.has_paid(USER)
    sale = [t for t in admin_texts(shop) if "Новая продажа" in t]
    assert len(sale) == 1 and "tiktok" in sale[0] and "тестов" not in sale[0]


async def test_wrong_amount_is_not_delivered_and_alerts_admin(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert await paid(shop, notification(pid, total="1.00"))
    assert shop.bot.documents == [] and not await shop.db.has_paid(USER)
    assert any("1.00" in t and "не выдал" in t for t in admin_texts(shop))


async def test_unsuccessful_status_is_ignored(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert await paid(shop, notification(pid, status="order_canceled"))
    assert shop.bot.documents == [] and not await shop.db.has_paid(USER)


async def test_lost_order_is_restored_from_notification(shop):
    """Заказа нет в базе (потерялась при переезде) — оплата всё равно не пропадает."""
    order = f"5000-{USER}"
    assert await paid(shop, notification(order))
    assert len(shop.bot.documents) == 1
    nxt, _ = await shop.get_or_create_payment(333)
    assert int(nxt.split("-")[0]) > 5000


async def test_unknown_order_without_buyer_alerts_admin(shop):
    assert await paid(shop, notification("garbage"))
    assert shop.bot.documents == []
    assert any("заказа нет в базе" in t for t in admin_texts(shop))


async def test_amount_formats_are_equal():
    assert _same_amount("890", "890.00") and _same_amount("890.000000", "890.00")
    assert not _same_amount("889.99", "890.00") and not _same_amount("abc", "890.00") and not _same_amount(None, "1")


async def test_notification_is_acknowledged_without_waiting_for_telegram(shop):
    slow = shop.bot.send_document

    async def slow_send(*a, **kw):
        await asyncio.sleep(2)
        return await slow(*a, **kw)
    shop.bot.send_document = slow_send
    pid, _ = await shop.get_or_create_payment(USER)
    t = time.monotonic()
    assert await shop.handle_notification(notification(pid))
    assert time.monotonic() - t < 1
    assert (await shop.db.get_payment(pid))["status"] == "succeeded"
    assert await shop.process_payment(pid) == "in_progress"
    await shop.wait_background()
    assert len(shop.bot.documents) == 1


async def test_check_after_delivery_reports_already_delivered(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert await shop.process_payment(pid) == "pending"
    await paid(shop, notification(pid))
    assert await shop.process_payment(pid) == "already_delivered"
    assert await shop.process_payment("nope") == "unknown"
    assert len(shop.bot.documents) == 1


async def test_repeat_payment_alerts_admin(shop):
    pid1, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid1))
    await shop.db._write("UPDATE payments SET created_at='2000-01-01' WHERE payment_id=?", (pid1,))
    pid2, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid2))
    assert len(shop.bot.documents) == 2
    assert any("повторно" in t for chat, t in shop.bot.messages if chat == USER)
    assert any("Повторная оплата" in t for t in admin_texts(shop))


async def test_manual_confirmation(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert await shop.confirm_manually(pid) == "succeeded"
    assert await shop.confirm_manually(pid) == "already_delivered"
    assert await shop.confirm_manually("1-1") == "unknown"
    assert len(shop.bot.documents) == 1


# ---------- повторная выдача ----------
async def test_failed_delivery_is_retried(shop, monkeypatch):
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 2
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid))
    assert shop.bot.documents == []
    await shop.poll_once()
    assert shop.bot.documents == []
    await shop.poll_once()
    assert len(shop.bot.documents) == 1
    thanks = [t for chat, t in shop.bot.messages if chat == USER and "Оплата прошла" in t]
    errors = [t for chat, t in shop.bot.messages if chat == USER and "техническ" in t]
    alerts = [t for t in admin_texts(shop) if "Не удалось выдать" in t]
    assert len(thanks) == 1 and len(errors) == 1 and len(alerts) == 1
    assert (await shop.db.stats())["undelivered"] == 0
    await shop.poll_once()
    assert len(shop.bot.documents) == 1


async def test_retry_waits_for_backoff(shop, monkeypatch):
    shop.bot.fail_documents = 1
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid))
    await shop.poll_once()
    assert shop.bot.documents == []
    later = dbmod.now() + timedelta(seconds=20)
    monkeypatch.setattr(dbmod, "now", lambda: later)
    await shop.poll_once()
    assert len(shop.bot.documents) == 1


async def test_delivery_gives_up_and_returning_user_still_gets_guide(shop, monkeypatch):
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 10 ** 6
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid))
    for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
        await shop.poll_once()
    assert (await shop.db.get_payment(pid))["delivery_attempts"] == MAX_DELIVERY_ATTEMPTS
    assert len([t for t in admin_texts(shop) if "Не удалось выдать" in t]) == 2
    shop.bot.fail_documents = 0
    await shop.poll_once()
    assert shop.bot.documents == []
    assert await shop.deliver_pending_for_user(USER)          # пользователь вернулся в бота
    assert len(shop.bot.documents) == 1


async def test_restart_during_sending_is_redelivered(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    await shop.db.mark_paid(pid)
    await shop.db._write("UPDATE payments SET delivered=? WHERE payment_id=?", (SENDING, pid))
    await shop.db.close()
    await shop.db.connect()
    await shop.poll_once()
    assert len(shop.bot.documents) == 1
    assert (await shop.db.get_payment(pid))["delivered"] == 1


# ---------- файл гайда ----------
async def test_watermark_keeps_pages_and_bookmarks(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid))
    stamped = PdfReader(io.BytesIO(shop.bot.documents[0][1].data))
    original = PdfReader(str(shop.cfg.product_file))
    assert len(stamped.pages) == len(original.pages)
    assert len(stamped.outline) == len(original.outline)
    assert "@ivan" in stamped.pages[-1].extract_text()


async def test_watermarked_copy_is_built_once(shop, monkeypatch):
    import bot.service as service
    calls = []
    real = service.stamp_pdf
    monkeypatch.setattr(service, "stamp_pdf", lambda *a: calls.append(1) or real(*a))
    for _ in range(3):
        await shop.send_product(USER)
    assert len(calls) == 1 and shop.bot.documents[1][1] == "FILEID"


async def test_watermark_failure_still_delivers(shop, monkeypatch):
    def broken(*a):
        raise RuntimeError("no font")
    monkeypatch.setattr("bot.service.stamp_pdf", broken)
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid))
    assert shop.bot.documents[0][1].data == shop.cfg.product_file.read_bytes()
    assert any("водяной знак" in t for t in admin_texts(shop))


async def test_stale_file_id_is_rebuilt(shop):
    await shop.send_product(USER)
    real = shop.bot.send_document

    async def picky(chat_id, document, **kw):
        if document == "FILEID":
            raise RuntimeError("Bad Request: wrong file identifier/HTTP URL specified")
        return await real(chat_id, document, **kw)
    shop.bot.send_document = picky
    await shop.send_product(USER)
    assert len(shop.bot.documents) == 2


async def test_network_error_on_cached_file_is_not_hidden(shop):
    await shop.send_product(USER)

    async def down(*a, **kw):
        raise RuntimeError("Forbidden: bot was blocked by the user")
    shop.bot.send_document = down
    with pytest.raises(RuntimeError):
        await shop.send_product(USER)


async def test_without_watermark_file_id_is_reused(shop):
    shop.cfg.watermark = False
    await shop.send_product(USER)
    await shop.send_product(USER)
    assert shop.bot.documents[1][1] == "FILEID"


async def test_grant(shop, monkeypatch):
    assert await shop.grant(222) == "succeeded"
    assert await shop.db.has_paid(222) and len(shop.bot.documents) == 1
    s = await shop.db.stats()
    assert s["sales"] == 0 and s["manual"] == 1
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 1
    assert await shop.grant(333) == "delivery_failed"
    await shop.poll_once()
    assert len(shop.bot.documents) == 2


# ---------- статистика, база, копии ----------
async def test_test_mode_payments_not_counted_as_revenue(shop):
    shop.cfg.test_mode = True
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, notification(pid))
    s = await shop.db.stats()
    assert s["sales"] == 0 and s["revenue"] == 0 and s["test_sales"] == 1
    assert any("тестовый платёж" in t for t in admin_texts(shop))
    shop.cfg.test_mode = False
    pid2, _ = await shop.get_or_create_payment(222)
    await paid(shop, notification(pid2))
    s = await shop.db.stats()
    assert s["sales"] == 1 and s["revenue"] == 890


async def test_old_database_is_migrated(tmp_path):
    import aiosqlite
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as c:
        await c.executescript("""
            CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                                source TEXT, created_at TEXT NOT NULL);
            CREATE TABLE payments (payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount TEXT NOT NULL,
                status TEXT NOT NULL, confirmation_url TEXT, created_at TEXT NOT NULL, paid_at TEXT,
                delivered INTEGER NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 0);
            INSERT INTO payments VALUES ('1001', 1, '890.00', 'succeeded', NULL, '2026-10-20T00:00:00+00:00',
                                         '2026-10-20T00:00:00+00:00', 1, 0);""")
    db = Database(path)
    await db.connect()
    try:
        row = await db.get_payment("1001")
        assert row["is_test"] == 0 and row["delivery_attempts"] == 0 and row["next_attempt_at"] is None
        assert (await db.stats())["sales"] == 1
    finally:
        await db.close()


async def test_daily_backup(shop):
    await shop.poll_once()
    folder = shop.cfg.db_path.parent / "backups"
    files = list(folder.glob("bot-*.db"))
    assert len(files) == 1 and files[0].stat().st_size > 0
    for i in range(20):
        (folder / f"bot-2000-01-{i + 1:02d}.db").write_bytes(b"x")
    shop._last_backup = None
    await shop.poll_once()
    assert len(list(folder.glob("bot-*.db"))) == 14


# ---------- веб-адреса ----------
async def test_web_notify_endpoint(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    data = notification(pid)
    body = form_body(data)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Sign": sign(data, SECRET)}
    async with TestClient(TestServer(make_app(shop))) as client:
        bad = await client.post("/pay/notify", data=body, headers={**headers, "Sign": "0" * 64})
        assert bad.status == 400 and shop.bot.documents == []
        nosign = await client.post("/pay/notify", data=body, headers={"Content-Type": headers["Content-Type"]})
        assert nosign.status == 400
        tampered = body.replace("890.00", "1.00")
        assert (await client.post("/pay/notify", data=tampered, headers=headers)).status == 400
        ok = await client.post("/pay/notify", data=body, headers=headers)
        assert ok.status == 200 and await ok.text() == "success"
        again = await client.post("/pay/notify", data=body, headers=headers)
        assert again.status == 200
        await shop.wait_background()
        assert (await client.get("/pay/notify")).status == 405
        succ = await client.get("/pay/success", allow_redirects=False)
        assert succ.status == 302 and succ.headers["Location"] == "https://t.me/guide_bot?start=paid"
        back = await client.get("/pay/return", allow_redirects=False)
        assert back.headers["Location"] == "https://t.me/guide_bot?start=fail"
        assert (await client.get("/health")).status == 200
        big = await client.post("/pay/notify", data="x=" + "a" * 300_000, headers=headers)
        assert big.status == 413
    assert len(shop.bot.documents) == 1
    saved = list((shop.cfg.db_path.parent / "rejected_notifications").glob("*.txt"))
    assert len(saved) == 3
    assert len([t for t in admin_texts(shop) if "не смог проверить" in t]) == 1   # без спама


async def test_web_notify_json_body(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    data = notification(pid)
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.post("/pay/notify", json=data, headers={"Sign": sign(data, SECRET)})
        assert r.status == 200
    await shop.wait_background()
    assert len(shop.bot.documents) == 1


async def test_web_notify_when_payments_disabled(shop):
    shop.cfg.pay_secret = ""
    async with TestClient(TestServer(make_app(shop))) as client:
        assert (await client.post("/pay/notify", data="a=1")).status == 503


async def test_offer_page(shop):
    from bot.offer import missing_fields, render_offer
    shop.cfg.seller_name, shop.cfg.seller_inn = "Малютин Максим", "701760448883"
    shop.cfg.seller_email, shop.cfg.support_username = "ivan@example.com", "help_me"
    page = render_offer(shop.cfg, "guide_bot")
    for part in ("Малютин Максим", "ИНН 701760448883", "ivan@example.com", "@help_me", "@guide_bot", "890 рублей", "Prodamus"):
        assert part in page, part
    assert "{" not in page.split("<main>")[1] and "НЕ ЗАПОЛНЕНО" not in page and "Robokassa" not in page
    assert missing_fields(shop.cfg) == []
    shop.cfg.seller_inn = '<script>"'
    assert "<script>" not in render_offer(shop.cfg, "guide_bot").split("<main>")[1]
    shop.cfg.seller_inn = ""
    assert "НЕ ЗАПОЛНЕНО" in render_offer(shop.cfg, "guide_bot") and missing_fields(shop.cfg) == ["SELLER_INN"]
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.get("/offer")
        assert r.status == 200 and "text/html" in r.headers["Content-Type"] and "Публичная оферта" in await r.text()
        assert r.headers["X-Content-Type-Options"] == "nosniff" and "default-src 'none'" in r.headers["Content-Security-Policy"]
