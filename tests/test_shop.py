"""Тесты оплаты через Робокассу и выдачи гайда — без сети и реальных денег."""
import asyncio
import hashlib
import io
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfReader

from bot.config import Config
from bot.db import Database
from bot.robokassa import Robokassa, parse_op_state
from bot.service import Shop
from bot.web import make_app

USER = 111
LOGIN, P1, P2 = "aibezlesa", "pass_one", "pass_two"


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class FakeKassa(Robokassa):
    """Настоящие подписи, но статус платежа берём из словаря вместо сети."""
    def __init__(self):
        super().__init__(LOGIN, P1, P2, is_test=True)
        self.states = {}

    async def op_state(self, inv_id):
        return self.states.get(str(inv_id), {"result": 3, "state": None, "out_sum": None})


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
    cfg.rk_login, cfg.rk_password1, cfg.rk_password2, cfg.rk_test = LOGIN, P1, P2, True
    cfg.db_path = tmp_path / "t.db"
    cfg.product_file = product_file
    cfg.admin_ids = [999]
    cfg.price_rub = 890
    cfg.watermark = True
    cfg.rk_send_receipt = False
    return cfg


@pytest.fixture
async def shop(tmp_path, sample_pdf):
    cfg = make_cfg(tmp_path, sample_pdf)
    db = Database(cfg.db_path)
    await db.connect()
    await db.upsert_user(USER, "ivan", "Иван", "tiktok")
    s = Shop(cfg, db, FakeKassa(), FakeBot())
    s.bot_username = "guide_bot"
    yield s
    await db.close()


async def paid(shop, data):
    """Уведомление Робокассы + ожидание фоновой выдачи гайда."""
    ok = await shop.handle_result(data)
    await shop.wait_background()
    return ok


def result_params(inv_id, out_sum="890.000000", user=USER, password=P2):
    return {"OutSum": out_sum, "InvId": str(inv_id), "Shp_user": str(user),
            "SignatureValue": md5(f"{out_sum}:{inv_id}:{password}:Shp_user={user}").upper()}


# ---------- подписи ----------
def test_payment_url_signature():
    k = Robokassa(LOGIN, P1, P2, is_test=True)
    url = k.payment_url(1001, "890.00", "Гайд ИИ без тёмного леса, PDF", {"Shp_user": 111})
    q = {k_: v[0] for k_, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://auth.robokassa.ru/Merchant/Index.aspx?")
    assert q["SignatureValue"] == md5(f"{LOGIN}:890.00:1001:{P1}:Shp_user=111")
    assert q["IsTest"] == "1" and q["Shp_user"] == "111" and q["OutSum"] == "890.00"
    live = Robokassa(LOGIN, P1, P2, is_test=False).payment_url(5, "890.00", "x", {"Shp_user": 1})
    assert "IsTest" not in live


def test_payment_url_sha256_and_shp_order():
    k = Robokassa(LOGIN, P1, P2, algo="sha256")
    url = k.payment_url(7, "890.00", "x", {"Shp_user": 1, "Shp_a": "z"})
    q = {k_: v[0] for k_, v in parse_qs(urlparse(url).query).items()}
    expected = hashlib.sha256(f"{LOGIN}:890.00:7:{P1}:Shp_a=z:Shp_user=1".encode()).hexdigest()
    assert q["SignatureValue"] == expected


def test_result_signature_check():
    k = Robokassa(LOGIN, P1, P2)
    assert k.check_result_signature(result_params(1001))
    assert not k.check_result_signature(result_params(1001, password=P1))
    tampered = result_params(1001)
    tampered["OutSum"] = "1.000000"
    assert not k.check_result_signature(tampered)
    assert not k.check_result_signature({})


def test_parse_op_state():
    xml = """<?xml version="1.0" encoding="utf-8"?>
<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/">
  <Result><Code>0</Code></Result>
  <State><Code>100</Code><RequestDate>2026-10-20T10:00:00</RequestDate></State>
  <Info><IncCurrLabel>BankCard</IncCurrLabel><IncSum>890.00</IncSum><OutCurrLabel>BankCard</OutCurrLabel><OutSum>890.00</OutSum></Info>
</OperationStateResponse>"""
    assert parse_op_state(xml) == {"result": 0, "state": 100, "out_sum": "890.00"}
    notfound = '<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/"><Result><Code>3</Code></Result></OperationStateResponse>'
    assert parse_op_state(notfound) == {"result": 3, "state": None, "out_sum": None}


# ---------- создание счёта ----------
async def test_create_reuses_link_and_unique_ids(shop):
    pid1, url1 = await shop.get_or_create_payment(USER)
    pid2, url2 = await shop.get_or_create_payment(USER)
    assert pid1 == pid2 and url1 == url2
    pid3, _ = await shop.get_or_create_payment(222)
    assert int(pid3) == int(pid1) + 1


# ---------- Result URL ----------
async def test_result_delivers_exactly_once(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    data = result_params(pid)
    results = await asyncio.gather(*[shop.handle_result(data) for _ in range(5)],
                                   *[shop.process_payment(pid) for _ in range(3)])
    await shop.wait_background()
    assert all(r in (True, "succeeded", "pending", "already_delivered", "in_progress") for r in results)
    assert len(shop.bot.documents) == 1
    assert await shop.db.has_paid(USER)
    sale = [m for m in shop.bot.messages if m[0] == 999]
    assert len(sale) == 1 and "тестовый платёж" in sale[0][1] and "tiktok" in sale[0][1]


async def test_result_wrong_amount_rejected(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert not await paid(shop, result_params(pid, out_sum="1.00"))
    assert shop.bot.documents == [] and not await shop.db.has_paid(USER)


async def test_result_wrong_user_rejected(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert not await paid(shop, result_params(pid, user=555))
    assert shop.bot.documents == []


async def test_result_restores_lost_invoice(shop):
    """Счёта нет в базе (потерялась при переезде) — оплата всё равно не пропадает."""
    assert await paid(shop, result_params(5000))
    assert len(shop.bot.documents) == 1
    nxt, _ = await shop.get_or_create_payment(333)
    assert int(nxt) > 5000  # номер не повторится


async def test_failed_delivery_is_retried(shop):
    shop.bot.fail_documents = 1
    pid, _ = await shop.get_or_create_payment(USER)
    assert await paid(shop, result_params(pid))
    assert shop.bot.documents == [] and any(m[0] == 999 for m in shop.bot.messages)
    assert await shop.process_payment(pid) == "succeeded"   # кнопка «проверить» или фоновая проверка
    assert len(shop.bot.documents) == 1


# ---------- запасная проверка статуса ----------
async def test_op_state_paid(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert await shop.process_payment(pid) == "pending"
    shop.kassa.states[pid] = {"result": 0, "state": 100, "out_sum": "890.00"}
    assert await shop.process_payment(pid) == "succeeded"
    assert len(shop.bot.documents) == 1


async def test_op_state_wrong_sum_and_cancel(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    shop.kassa.states[pid] = {"result": 0, "state": 100, "out_sum": "10.00"}
    assert await shop.process_payment(pid) == "unknown"
    shop.kassa.states[pid] = {"result": 0, "state": 10, "out_sum": None}
    assert await shop.process_payment(pid) == "canceled"
    pid2, _ = await shop.get_or_create_payment(USER)
    assert pid2 != pid and shop.bot.documents == []


# ---------- водяной знак, выдача вручную ----------
async def test_watermark_keeps_pages_and_bookmarks(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid))
    stamped = PdfReader(io.BytesIO(shop.bot.documents[0][1].data))
    original = PdfReader(str(shop.cfg.product_file))
    assert len(stamped.pages) == len(original.pages)
    assert len(stamped.outline) == len(original.outline)
    assert "@ivan" in stamped.pages[5].extract_text()


async def test_grant(shop):
    await shop.grant(222)
    assert await shop.db.has_paid(222) and len(shop.bot.documents) == 1
    s = await shop.db.stats()
    assert s["sales"] == 0 and s["manual"] == 1


# ---------- веб-адреса ----------
async def test_web_endpoints(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    async with TestClient(TestServer(make_app(shop))) as client:
        bad = await client.post("/robokassa/result", data=result_params(pid, password="hacker"))
        assert bad.status == 400 and shop.bot.documents == []
        ok = await client.post("/robokassa/result", data=result_params(pid))
        assert ok.status == 200 and await ok.text() == f"OK{pid}"
        again = await client.post("/robokassa/result", data=result_params(pid))
        assert again.status == 200
        await shop.wait_background()
        got = await client.get("/robokassa/result", params=result_params(pid))  # если в кабинете выбран GET
        assert got.status == 200
        succ = await client.get("/robokassa/success?OutSum=890&InvId=1", allow_redirects=False)
        assert succ.status == 302 and succ.headers["Location"] == "https://t.me/guide_bot?start=paid"
        fail = await client.get("/robokassa/fail", allow_redirects=False)
        assert fail.headers["Location"] == "https://t.me/guide_bot?start=fail"
        assert (await client.get("/health")).status == 200
    assert len(shop.bot.documents) == 1


# ---------- оферта ----------
async def test_offer_page(shop):
    from bot.offer import missing_fields, render_offer
    shop.cfg.seller_name, shop.cfg.seller_inn = "Иванов Иван Иванович", "123456789012"
    shop.cfg.seller_email, shop.cfg.support_username = "ivan@example.com", "help_me"
    page = render_offer(shop.cfg, "guide_bot")
    for part in ("Иванов Иван Иванович", "ИНН 123456789012", "ivan@example.com", "@help_me", "@guide_bot", "890 рублей", "Robokassa"):
        assert part in page, part
    assert "{" not in page.split("<main>")[1] and "НЕ ЗАПОЛНЕНО" not in page
    assert missing_fields(shop.cfg) == []
    shop.cfg.seller_inn = '<script>"'
    assert "<script>" not in render_offer(shop.cfg, "guide_bot").split("<main>")[1]
    shop.cfg.seller_inn = ""
    assert "НЕ ЗАПОЛНЕНО" in render_offer(shop.cfg, "guide_bot") and missing_fields(shop.cfg) == ["SELLER_INN"]
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.get("/offer")
        assert r.status == 200 and "text/html" in r.headers["Content-Type"] and "Публичная оферта" in await r.text()
        assert r.headers["X-Content-Type-Options"] == "nosniff" and "default-src 'none'" in r.headers["Content-Security-Policy"]
        big = await client.post("/robokassa/result", data={"x": "a" * 100_000})
        assert big.status == 413


def test_config_defaults_offer_and_seller(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://1-2-3-4.sslip.io/")
    monkeypatch.setenv("OFFER_URL", "")
    monkeypatch.setenv("SELLER_NAME", "Иванов Иван")
    monkeypatch.setenv("SELLER_INN", "123")
    monkeypatch.setenv("SELLER_INFO", "")
    c = Config()
    assert c.offer_url == "https://1-2-3-4.sslip.io/offer"
    assert c.seller_info == "Иванов Иван, самозанятый, ИНН 123"
    monkeypatch.setenv("OFFER_URL", "https://telegra.ph/my")
    assert Config().offer_url == "https://telegra.ph/my"


# ---------- номера счетов ----------
async def test_invoice_ids_survive_database_loss(shop, tmp_path, monkeypatch):
    """После потери базы номера счетов не повторяют старые — иначе проверка статуса
    могла бы найти чужой оплаченный счёт с тем же номером."""
    import time
    pid, _ = await shop.get_or_create_payment(USER)
    assert int(pid) >= int(time.time()) - 5
    later = time.time() + 60  # переустановка бота произошла позже
    monkeypatch.setattr("bot.db.time.time", lambda: later)
    fresh = Database(tmp_path / "new.db")
    await fresh.connect()
    try:
        assert await fresh.next_inv_id() > int(pid)
    finally:
        await fresh.close()


async def test_price_change_creates_new_invoice(shop):
    pid1, url1 = await shop.get_or_create_payment(USER)
    shop.cfg.price_rub = 990
    pid2, url2 = await shop.get_or_create_payment(USER)
    assert pid1 != pid2 and "OutSum=990.00" in url2
    shop.cfg.rk_test = shop.kassa.is_test = False
    pid3, url3 = await shop.get_or_create_payment(USER)
    assert pid3 != pid2 and "IsTest" not in url3


async def test_double_click_creates_one_invoice(shop):
    results = await asyncio.gather(*[shop.get_or_create_payment(USER) for _ in range(5)])
    assert len({pid for pid, _ in results}) == 1
    assert shop._locks == {}  # блокировки не копятся


# ---------- повторная выдача ----------
async def test_undelivered_payment_is_retried_by_poller(shop, monkeypatch):
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 2
    pid, _ = await shop.get_or_create_payment(USER)
    assert await paid(shop, result_params(pid))
    assert shop.bot.documents == []
    await shop.poll_once()                 # вторая неудача
    assert shop.bot.documents == []
    await shop.poll_once()                 # получилось
    assert len(shop.bot.documents) == 1
    thanks = [m for m in shop.bot.messages if m[0] == USER and "Оплата прошла" in m[1]]
    errors = [m for m in shop.bot.messages if m[0] == USER and "техническ" in m[1]]
    admin_alerts = [m for m in shop.bot.messages if m[0] == 999 and "Не удалось выдать" in m[1]]
    assert len(thanks) == 1 and len(errors) == 1 and len(admin_alerts) == 1
    assert (await shop.db.stats())["undelivered"] == 0
    await shop.poll_once()
    assert len(shop.bot.documents) == 1


async def test_delivery_gives_up_after_max_attempts(shop, monkeypatch):
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    from bot.service import MAX_DELIVERY_ATTEMPTS
    shop.bot.fail_documents = 10 ** 6
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid))
    for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
        await shop.poll_once()
    row = await shop.db.get_payment(pid)
    assert row["delivery_attempts"] == MAX_DELIVERY_ATTEMPTS
    alerts = [m for m in shop.bot.messages if m[0] == 999 and "Не удалось выдать" in m[1]]
    assert len(alerts) == 2  # первая неудача и последняя попытка


async def test_check_after_delivery_reports_already_delivered(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid))
    assert await shop.process_payment(pid) == "already_delivered"
    assert len(shop.bot.documents) == 1


# ---------- кэш файла ----------
async def test_watermarked_copy_is_built_once(shop, monkeypatch):
    import bot.service as service
    calls = []
    real = service.stamp_pdf
    monkeypatch.setattr(service, "stamp_pdf", lambda *a: calls.append(1) or real(*a))
    for _ in range(3):
        await shop.send_product(USER)
    assert len(calls) == 1 and shop.bot.documents[1][1] == "FILEID"


# ---------- статистика ----------
async def test_test_payments_not_counted_as_revenue(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid))
    s = await shop.db.stats()
    assert s["sales"] == 0 and s["revenue"] == 0 and s["test_sales"] == 1
    shop.cfg.rk_test = shop.kassa.is_test = False
    pid2, _ = await shop.get_or_create_payment(222)
    await paid(shop, result_params(pid2, user=222))
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
        assert row["is_test"] == 0 and row["delivery_attempts"] == 0
        assert (await db.stats())["sales"] == 1
    finally:
        await db.close()


# ---------- второй аудит: надёжность пути «оплата → гайд» ----------
async def test_many_buyers_at_once_get_unique_invoices(shop):
    """Раньше параллельные покупки разных пользователей получали один номер счёта."""
    for uid in range(1, 51):
        await shop.db.upsert_user(uid, None, "U", None)
    results = await asyncio.gather(*[shop.get_or_create_payment(uid) for uid in range(1, 51)])
    assert len({pid for pid, _ in results}) == 50


async def test_result_is_acknowledged_without_waiting_for_telegram(shop):
    """Медленный Telegram не задерживает ответ Робокассе: оплата фиксируется сразу, гайд — в фоне."""
    import time
    slow = shop.bot.send_document

    async def slow_send(*a, **kw):
        await asyncio.sleep(3)
        return await slow(*a, **kw)
    shop.bot.send_document = slow_send
    pid, _ = await shop.get_or_create_payment(USER)
    t = time.monotonic()
    assert await shop.handle_result(result_params(pid))
    assert time.monotonic() - t < 1
    assert (await shop.db.get_payment(pid))["status"] == "succeeded"
    assert await shop.process_payment(pid) == "in_progress"
    await shop.wait_background()
    assert len(shop.bot.documents) == 1


async def test_restart_during_sending_is_redelivered(shop, tmp_path):
    """Бот перезапустился посреди отправки — гайд не теряется."""
    from bot.db import SENDING
    pid, _ = await shop.get_or_create_payment(USER)
    await shop.db.mark_paid(pid)
    await shop.db._write("UPDATE payments SET delivered=? WHERE payment_id=?", (SENDING, pid))
    await shop.db.close()
    await shop.db.connect()                      # запуск после сбоя
    await shop.poll_once()
    assert len(shop.bot.documents) == 1
    assert (await shop.db.get_payment(pid))["delivered"] == 1


async def test_retry_waits_for_backoff(shop, monkeypatch):
    from datetime import timedelta

    import bot.db as dbmod
    shop.bot.fail_documents = 1
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid))
    await shop.poll_once()
    assert shop.bot.documents == []              # пауза ещё не прошла
    later = dbmod.now() + timedelta(seconds=20)
    monkeypatch.setattr(dbmod, "now", lambda: later)
    await shop.poll_once()
    assert len(shop.bot.documents) == 1


async def test_user_returning_gets_guide_even_after_attempts_exhausted(shop, monkeypatch):
    """Пользователь заблокировал бота, попытки кончились, потом он вернулся — гайд приходит сразу."""
    from bot.service import MAX_DELIVERY_ATTEMPTS
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 10 ** 6
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid))
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        await shop.poll_once()
    shop.bot.fail_documents = 0
    await shop.poll_once()
    assert shop.bot.documents == []
    assert await shop.deliver_pending_for_user(USER)
    assert len(shop.bot.documents) == 1


async def test_late_payment_is_found_by_poller(shop):
    """Покупатель оплатил старую ссылку (позже TTL), а уведомление не дошло."""
    from datetime import timedelta

    import bot.db as dbmod
    pid, _ = await shop.get_or_create_payment(USER)
    old = dbmod.iso(dbmod.now() - timedelta(hours=3))
    await shop.db._write("UPDATE payments SET created_at=? WHERE payment_id=?", (old, pid))
    shop.kassa.states[pid] = {"result": 0, "state": 100, "out_sum": "890.00"}
    calls = []
    real = shop.kassa.op_state

    async def counting(inv):
        calls.append(inv)
        return await real(inv)
    shop.kassa.op_state = counting
    await shop.poll_once()
    assert len(shop.bot.documents) == 1 and calls == [pid]


async def test_old_unpaid_invoice_is_checked_rarely(shop):
    from datetime import timedelta

    import bot.db as dbmod
    pid, _ = await shop.get_or_create_payment(USER)
    await shop.db._write("UPDATE payments SET created_at=? WHERE payment_id=?",
                         (dbmod.iso(dbmod.now() - timedelta(hours=3)), pid))
    calls = []

    async def counting(inv):
        calls.append(inv)
        return {"result": 3, "state": None, "out_sum": None}
    shop.kassa.op_state = counting
    for _ in range(5):
        await shop.poll_once()
    assert calls == [pid]                        # не чаще раза в 10 минут
    await shop.db._write("UPDATE payments SET created_at=?, checked_at=NULL WHERE payment_id=?",
                         (dbmod.iso(dbmod.now() - timedelta(hours=30)), pid))
    await shop.poll_once()
    assert calls == [pid]                        # старше суток — не проверяется


async def test_paid_after_cancel_is_delivered(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    shop.kassa.states[pid] = {"result": 0, "state": 10, "out_sum": None}
    assert await shop.process_payment(pid) == "canceled"
    assert await paid(shop, result_params(pid))
    assert len(shop.bot.documents) == 1
    await shop.db.mark_canceled(pid)             # оплаченный счёт отменить нельзя
    assert (await shop.db.get_payment(pid))["status"] == "succeeded"


async def test_repeat_payment_alerts_admin(shop):
    pid1, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid1))
    await shop.db._write("UPDATE payments SET created_at='2000-01-01' WHERE payment_id=?", (pid1,))
    pid2 = str(int(pid1) + 100)
    await paid(shop, result_params(pid2))        # оплатил вторую, старую ссылку
    assert len(shop.bot.documents) == 2
    assert any("повторно" in m[1] for m in shop.bot.messages if m[0] == USER)
    assert any("Повторная оплата" in m[1] for m in shop.bot.messages if m[0] == 999)


async def test_rejected_signed_notification_alerts_admin(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert not await paid(shop, result_params(pid, out_sum="1.000000"))
    assert any("отклонил" in m[1] for m in shop.bot.messages if m[0] == 999)


async def test_amount_formats_are_equal(shop):
    from bot.service import _same_amount
    assert _same_amount("890", "890.00") and _same_amount("890.000000", "890.00")
    assert not _same_amount("889.99", "890.00") and not _same_amount("abc", "890.00") and not _same_amount(None, "1")


async def test_watermark_failure_still_delivers(shop, monkeypatch):
    def broken(*a):
        raise RuntimeError("no font")
    monkeypatch.setattr("bot.service.stamp_pdf", broken)
    pid, _ = await shop.get_or_create_payment(USER)
    await paid(shop, result_params(pid))
    assert len(shop.bot.documents) == 1
    assert shop.bot.documents[0][1].data == shop.cfg.product_file.read_bytes()
    assert any("водяной знак" in m[1] for m in shop.bot.messages if m[0] == 999)


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


async def test_grant_reports_failure(shop, monkeypatch):
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 1
    assert await shop.grant(222) == "delivery_failed"
    await shop.poll_once()
    assert len(shop.bot.documents) == 1


async def test_live_mode_rejects_test_notifications(shop):
    shop.cfg.rk_test = shop.kassa.is_test = False
    pid, _ = await shop.get_or_create_payment(USER)
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.post("/robokassa/result", data={**result_params(pid), "IsTest": "1"})
        assert r.status == 400
        r = await client.post("/robokassa/result", data=result_params(pid))
        assert r.status == 200
    await shop.wait_background()
    assert len(shop.bot.documents) == 1
