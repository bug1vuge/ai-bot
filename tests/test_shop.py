"""Тесты оплаты через Робокассу и выдачи гайда — без сети и реальных денег."""
import asyncio
import hashlib
import io
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfReader

from bot.config import BASE_DIR, Config
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


def make_cfg(tmp_path):
    cfg = Config()
    cfg.rk_login, cfg.rk_password1, cfg.rk_password2, cfg.rk_test = LOGIN, P1, P2, True
    cfg.db_path = tmp_path / "t.db"
    cfg.product_file = BASE_DIR / "data" / "guide.pdf"
    cfg.admin_ids = [999]
    cfg.price_rub = 890
    cfg.watermark = True
    cfg.rk_send_receipt = False
    return cfg


@pytest.fixture
async def shop(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    await db.connect()
    await db.upsert_user(USER, "ivan", "Иван", "tiktok")
    s = Shop(cfg, db, FakeKassa(), FakeBot())
    s.bot_username = "guide_bot"
    yield s
    await db.close()


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
    assert all(r in (True, "succeeded", "pending") for r in results)
    assert len(shop.bot.documents) == 1
    assert await shop.db.has_paid(USER)
    sale = [m for m in shop.bot.messages if m[0] == 999]
    assert len(sale) == 1 and "тестовый режим" in sale[0][1] and "tiktok" in sale[0][1]


async def test_result_wrong_amount_rejected(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert not await shop.handle_result(result_params(pid, out_sum="1.00"))
    assert shop.bot.documents == [] and not await shop.db.has_paid(USER)


async def test_result_wrong_user_rejected(shop):
    pid, _ = await shop.get_or_create_payment(USER)
    assert not await shop.handle_result(result_params(pid, user=555))
    assert shop.bot.documents == []


async def test_result_restores_lost_invoice(shop):
    """Счёта нет в базе (потерялась при переезде) — оплата всё равно не пропадает."""
    assert await shop.handle_result(result_params(5000))
    assert len(shop.bot.documents) == 1
    nxt, _ = await shop.get_or_create_payment(333)
    assert int(nxt) > 5000  # номер не повторится


async def test_failed_delivery_is_retried(shop):
    shop.bot.fail_documents = 1
    pid, _ = await shop.get_or_create_payment(USER)
    assert await shop.handle_result(result_params(pid))
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
    await shop.handle_result(result_params(pid))
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
