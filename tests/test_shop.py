"""Оплата через ЮKassa и выдача гайда: против поддельного, но настоящего по протоколу API ЮKassa.
Покрывает и нормальный путь, и попытки сломать оплату: поддельные уведомления, повторные
оплаты, подмену суммы и магазина, тестовые платежи, сбои связи."""
import asyncio
import io
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfReader

import bot.db as dbmod
from bot.config import Config
from bot.db import SENDING, Database
from bot.service import MAX_DELIVERY_ATTEMPTS, AlreadyPaid, PaymentInReview, Shop
from bot.web import make_app
from bot.yookassa import YooKassa, YooKassaError
from tests.fake_yookassa import SECRET, SHOP_ID

USER = 111


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


def make_cfg(tmp_path, product_file, api_url="http://127.0.0.1:1/v3"):
    cfg = Config()
    cfg.shop_id, cfg.secret_key, cfg.api_url = SHOP_ID, SECRET, api_url
    cfg.test_mode, cfg.check_ip, cfg.auto_refund_duplicates = False, True, True
    cfg.public_url = "https://bot.example.ru"
    cfg.db_path = tmp_path / "t.db"
    cfg.product_file = product_file
    cfg.admin_ids = [999]
    cfg.price_rub = 990
    cfg.watermark = True
    return cfg


@pytest.fixture
async def shop(tmp_path, sample_pdf, yk):
    cfg = make_cfg(tmp_path, sample_pdf, yk.url)
    db = Database(cfg.db_path)
    await db.connect()
    await db.upsert_user(USER, "ivan", "Иван", "tiktok")
    pay = YooKassa(cfg.shop_id, cfg.secret_key, cfg.api_url, timeout=1, backoff=0.01)
    s = Shop(cfg, db, pay, FakeBot())
    s.bot_username = "guide_bot"
    yield s
    await s.wait_background()
    await pay.close()
    await db.close()


async def notify(shop, order_id, event="payment.succeeded"):
    """Уведомление ЮKassa по заказу + ожидание фоновой выдачи."""
    row = await shop.db.get_payment(order_id)
    result = await shop.handle_notification(event, row["provider_id"])
    await shop.wait_background()
    return result


async def buy_and_pay(shop, yk, user=USER):
    order, _ = await shop.get_or_create_payment(user)
    yk.pay(yk.by_order(order)["id"])
    assert await notify(shop, order) == "paid"
    return order


def admin_texts(shop):
    return [t for chat, t in shop.bot.messages if chat == 999]


def user_texts(shop, user=USER):
    return [t for chat, t in shop.bot.messages if chat == user]


# ---------- создание платежа ----------
async def test_payment_request(shop, yk):
    order, url = await shop.get_or_create_payment(USER)
    method, path, body = yk.calls[-1]
    assert (method, path) == ("POST", "/payments")
    assert body["amount"] == {"value": "990.00", "currency": "RUB"} and body["capture"] is True
    assert body["confirmation"] == {"type": "redirect", "return_url": "https://bot.example.ru/pay/success"}
    assert body["metadata"] == {"order_id": order, "user_id": str(USER)}
    assert order in body["description"] and len(body["description"]) <= 128
    assert yk.idempotent.keys() == {order}                     # ключ идемпотентности = номер заказа
    assert url == yk.by_order(order)["confirmation"]["confirmation_url"]
    row = await shop.db.get_payment(order)
    assert row["provider_id"] == yk.by_order(order)["id"] and row["confirmation_url"] == url


async def test_repeat_buy_reuses_live_payment(shop, yk):
    o1, u1 = await shop.get_or_create_payment(USER)
    o2, u2 = await shop.get_or_create_payment(USER)
    assert (o1, u1) == (o2, u2) and yk.created == 1
    assert yk.count("GET /payments/") == 1                       # статус спросили у ЮKassa


async def test_double_click_creates_one_payment(shop, yk):
    results = await asyncio.gather(*[shop.get_or_create_payment(USER) for _ in range(6)])
    assert len(set(results)) == 1 and yk.created == 1
    assert shop._locks == {}


async def test_many_buyers_at_once_get_own_payments(shop, yk):
    for uid in range(1, 31):
        await shop.db.upsert_user(uid, None, "U", None)
    results = await asyncio.gather(*[shop.get_or_create_payment(uid) for uid in range(1, 31)])
    assert len({o for o, _ in results}) == 30 and yk.created == 30


async def test_canceled_payment_gets_new_link(shop, yk):
    o1, _ = await shop.get_or_create_payment(USER)
    yk.cancel(yk.by_order(o1)["id"])
    o2, _ = await shop.get_or_create_payment(USER)
    assert o2 != o1 and yk.created == 2
    assert (await shop.db.get_payment(o1))["status"] == "canceled"


async def test_buy_after_paying_finds_payment_instead_of_new_link(shop, yk):
    """Уведомление ещё не пришло, а покупатель снова жмёт «Купить» — второй платёж не создаётся."""
    o1, _ = await shop.get_or_create_payment(USER)
    yk.pay(yk.by_order(o1)["id"])
    with pytest.raises(AlreadyPaid):
        await shop.get_or_create_payment(USER)
    await shop.wait_background()
    assert yk.created == 1 and len(shop.bot.documents) == 1
    with pytest.raises(AlreadyPaid):
        await shop.get_or_create_payment(USER)
    assert yk.created == 1


async def test_lost_response_does_not_create_second_payment(shop, yk):
    """ЮKassa создала платёж, но ответ потерялся (таймаут). Повтор с тем же ключом идемпотентности
    возвращает тот же платёж — двух живых ссылок не бывает."""
    yk.slow_delay = 1.5
    yk.fail_next("POST /payments", "slow")
    order, url = await shop.get_or_create_payment(USER)        # клиент повторил запрос сам
    assert yk.created == 1 and yk.count("POST /payments") == 2
    assert url == yk.by_order(order)["confirmation"]["confirmation_url"]


async def test_yookassa_down_then_retry_with_same_key(shop, yk):
    yk.fail_next("POST /payments", 500, times=4)
    with pytest.raises(YooKassaError):
        await shop.get_or_create_payment(USER)
    assert yk.created == 0
    o2, url = await shop.get_or_create_payment(USER)           # тот же заказ, тот же ключ
    assert yk.created == 1 and len(yk.idempotent) == 1 and o2 in yk.idempotent
    assert (await shop.db.open_order(USER))["payment_id"] == o2


async def test_temporary_errors_are_retried(shop, yk):
    yk.fail_next("POST /payments", 202)
    yk.fail_next("POST /payments", 429)
    order, _ = await shop.get_or_create_payment(USER)
    assert yk.created == 1
    yk.fail_next("GET /payments/", 503, times=2)
    assert await shop.sync_order(order) == "pending"


async def test_bad_credentials_are_not_retried(shop, yk):
    await shop.pay.close()
    shop.pay = YooKassa(SHOP_ID, "live_" + "X" * 40, yk.url, backoff=0.01)
    with pytest.raises(YooKassaError) as e:
        await shop.get_or_create_payment(USER)
    assert e.value.status == 401 and yk.count("POST /payments") == 1


async def test_switching_shop_closes_old_order(shop, yk):
    """Платёж другого магазина (сменили тестовый на боевой) не находится — создаётся новый заказ."""
    o1, _ = await shop.get_or_create_payment(USER)
    yk.payments.clear()
    o2, _ = await shop.get_or_create_payment(USER)
    assert o2 != o1 and (await shop.db.get_payment(o1))["status"] == "canceled"


async def test_price_change_keeps_single_live_link(shop, yk):
    o1, u1 = await shop.get_or_create_payment(USER)
    shop.cfg.price_rub = 1490
    o2, u2 = await shop.get_or_create_payment(USER)
    assert (o1, u1) == (o2, u2) and yk.created == 1             # живая ссылка одна
    yk.cancel(yk.by_order(o1)["id"])
    o3, _ = await shop.get_or_create_payment(USER)
    assert (await shop.db.get_payment(o3))["amount"] == "1490.00"


# ---------- подтверждение оплаты ----------
async def test_paid_order_delivers_exactly_once(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    pid = yk.by_order(order)["id"]
    yk.pay(pid)
    results = await asyncio.gather(*[shop.handle_notification("payment.succeeded", pid) for _ in range(5)],
                                   *[shop.process_payment(order) for _ in range(3)], shop.poll_once())
    await shop.wait_background()
    assert len(shop.bot.documents) == 1
    assert results.count("paid") == 1
    sale = [t for t in admin_texts(shop) if "Новая продажа" in t]
    assert len(sale) == 1 and "990" in sale[0] and "tiktok" in sale[0]


async def test_forged_notification_does_nothing(shop, yk):
    """Уведомление «оплачено», а по API платёж не оплачен — гайд не выдаётся."""
    order, _ = await shop.get_or_create_payment(USER)
    assert await notify(shop, order) == "pending"
    assert shop.bot.documents == [] and not await shop.db.has_paid(USER)


async def test_notification_about_foreign_payment_is_ignored(shop, yk):
    calls = len(yk.calls)
    assert await shop.handle_notification("payment.succeeded", "2a0e8b3e-000f-5000-9000-1a2b3c4d5e6f") == "ignored"
    assert len(yk.calls) == calls                               # даже не спрашиваем ЮKassa


@pytest.mark.parametrize("change, reason", [
    ({"amount": {"value": "1.00", "currency": "RUB"}}, "сумма"),
    ({"amount": {"value": "990.00", "currency": "USD"}}, "сумма"),
    ({"metadata": {"order_id": "1-1", "user_id": "111"}}, "метаданные"),
    ({"metadata": "OTHER_USER"}, "метаданные"),
    ({"recipient": {"account_id": "999999"}}, "другого магазина"),
    ({"test": True}, "тестовый платёж"),
])
async def test_suspicious_payment_goes_to_review(shop, yk, change, reason):
    order, _ = await shop.get_or_create_payment(USER)
    pid = yk.by_order(order)["id"]
    if change.get("metadata") == "OTHER_USER":
        change = {"metadata": {"order_id": order, "user_id": "222"}}
    yk.pay(pid, **change)
    assert await notify(shop, order) == "review"
    assert shop.bot.documents == [] and not await shop.db.has_paid(USER)
    alerts = [t for t in admin_texts(shop) if "не выдал гайд" in t]
    assert len(alerts) == 1 and reason in alerts[0]
    await notify(shop, order)                                   # повтор — без повторной тревоги
    assert len([t for t in admin_texts(shop) if "не выдал гайд" in t]) == 1
    assert (await shop.db.stats())["review"] == 1
    with pytest.raises(PaymentInReview):                        # второй раз деньги не берём
        await shop.get_or_create_payment(USER)
    assert yk.created == 1


async def test_test_payments_accepted_in_test_mode(shop, yk):
    shop.cfg.test_mode = True
    yk.test_payments = True
    order = await buy_and_pay(shop, yk)
    assert len(shop.bot.documents) == 1
    s = await shop.db.stats()
    assert s["sales"] == 0 and s["test_sales"] == 1 and (await shop.db.get_payment(order))["is_test"] == 1


async def test_waiting_for_capture_is_captured(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    pid = yk.by_order(order)["id"]
    yk.wait_capture(pid)
    assert await notify(shop, order, "payment.waiting_for_capture") == "paid"
    assert yk.count(f"POST /payments/{pid}/capture") == 1 and len(shop.bot.documents) == 1


async def test_suspicious_hold_is_not_captured(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    pid = yk.by_order(order)["id"]
    yk.wait_capture(pid)
    yk.payments[pid]["amount"] = {"value": "1.00", "currency": "RUB"}
    assert await notify(shop, order, "payment.waiting_for_capture") == "review"
    assert yk.count(f"POST /payments/{pid}/capture") == 0 and shop.bot.documents == []
    assert any("не подтверждён" in t for t in admin_texts(shop))


async def test_canceled_notification(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    yk.cancel(yk.by_order(order)["id"])
    assert await notify(shop, order, "payment.canceled") == "canceled"
    assert (await shop.db.get_payment(order))["status"] == "canceled" and shop.bot.documents == []


async def test_poller_finds_payment_without_notification(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    yk.pay(yk.by_order(order)["id"])
    await shop.poll_once()
    await shop.wait_background()
    assert len(shop.bot.documents) == 1


async def test_late_payment_on_old_order_is_found(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    await shop.db._write("UPDATE payments SET created_at=? WHERE payment_id=?",
                         (dbmod.iso(dbmod.now() - timedelta(hours=5)), order))
    await shop.poll_once()
    gets = yk.count("GET /payments/")
    await shop.poll_once()
    assert yk.count("GET /payments/") == gets                   # старые — не чаще раза в 10 минут
    yk.pay(yk.by_order(order)["id"])
    await notify(shop, order)
    assert len(shop.bot.documents) == 1


async def test_check_button_finds_payment_on_expired_order(shop, yk):
    """Заказ закрыт у нас по сроку, покупатель оплатил в последний момент, уведомление потерялось."""
    order, _ = await shop.get_or_create_payment(USER)
    await shop.db.set_status(order, dbmod.CANCELED, note="истёк срок")
    yk.pay(yk.by_order(order)["id"])
    assert await shop.process_payment(order) in {"succeeded", "already_delivered"}
    assert len(shop.bot.documents) == 1
    assert (await shop.db.get_payment(order))["status"] == "succeeded"


async def test_changed_settings_after_lost_response_do_not_block_buying(shop, yk):
    """ЮKassa создала платёж, ответ потерялся, затем поменялись настройки (описание, адрес возврата):
    повтор с тем же ключом ЮKassa отклоняет — заказ закрывается, покупатель сразу получает новую ссылку."""
    yk.fail_next("POST /payments", "slow", times=4)
    with pytest.raises(YooKassaError):
        await shop.get_or_create_payment(USER)
    shop.cfg.payment_description = "Новое описание"
    order, url = await shop.get_or_create_payment(USER)   # конфликт ключа: старый заказ закрыт, создан новый
    assert url and (await shop.db.get_payment(order))["provider_id"]
    assert len([t for t in admin_texts(shop) if "отказывается создавать" in t]) == 1


async def test_rejected_payment_creation_alerts_admin_once(shop, yk):
    yk.secret = "live_" + "X" * 40   # ключ отозван в кабинете
    for _ in range(3):
        with pytest.raises(YooKassaError):
            await shop.get_or_create_payment(USER)
    assert len([t for t in admin_texts(shop) if "отказывается создавать" in t]) == 1
    assert await shop.db.open_order(USER) is None   # мёртвые заказы не висят
    yk.secret = SECRET
    _, url = await shop.get_or_create_payment(USER)
    assert url


@pytest.mark.parametrize("account, problem", [
    ({}, None),
    ({"account_id": "999999"}, "принадлежит магазину 999999"),
    ({"test": True}, "тестовый магазин"),
    ({"fiscalization_enabled": True}, "отправка чеков"),
    ({"status": "disabled"}, "в статусе"),
])
async def test_check_account(shop, yk, account, problem):
    yk.account = account
    result = await shop.check_account()
    assert result is None if problem is None else problem in result


async def test_check_account_with_wrong_key(shop, yk):
    yk.secret = "live_" + "X" * 40
    assert "не принимает" in await shop.check_account()


async def test_check_button_asks_yookassa(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    assert await shop.process_payment(order) == "pending"
    yk.pay(yk.by_order(order)["id"])
    assert await shop.process_payment(order) == "already_delivered"   # выдан в фоне за время проверки
    assert len(shop.bot.documents) == 1
    assert await shop.process_payment("nope") == "unknown"


async def test_notification_when_yookassa_api_is_down(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    yk.pay(yk.by_order(order)["id"])
    yk.fail_next("GET /payments/", 500, times=4)
    with pytest.raises(YooKassaError):
        await notify(shop, order)
    assert shop.bot.documents == []
    await notify(shop, order)                                   # ЮKassa повторила уведомление
    assert len(shop.bot.documents) == 1


# ---------- повторные оплаты и возвраты ----------
async def second_paid_order(shop, yk):
    """Покупатель открыл две ссылки: бот видел первую отменённой и выдал вторую, но первая тоже оплатилась."""
    o1, _ = await shop.get_or_create_payment(USER)
    yk.cancel(yk.by_order(o1)["id"])
    o2, _ = await shop.get_or_create_payment(USER)
    yk.pay(yk.by_order(o2)["id"])
    await notify(shop, o2)
    yk.pay(yk.by_order(o1)["id"])
    return o1


async def test_second_payment_is_refunded_automatically(shop, yk):
    o1 = await second_paid_order(shop, yk)
    assert await notify(shop, o1) == "duplicate"
    assert len(shop.bot.documents) == 1 and yk.refunds == 1
    assert yk.by_order(o1)["refunded_amount"]["value"] == "990.00"
    assert any("автоматически возвращаются" in t for t in user_texts(shop))
    assert any("Повторная оплата" in t for t in admin_texts(shop))
    s = await shop.db.stats()
    assert s["sales"] == 1 and s["revenue"] == 990 and s["duplicates"] == 1
    await notify(shop, o1)                                      # повтор уведомления — без второго возврата
    await shop.poll_once()
    assert yk.refunds == 1


async def test_simultaneous_double_payment_counts_once(shop, yk):
    o1, _ = await shop.get_or_create_payment(USER)
    yk.cancel(yk.by_order(o1)["id"])
    o2, _ = await shop.get_or_create_payment(USER)
    yk.pay(yk.by_order(o1)["id"])
    yk.pay(yk.by_order(o2)["id"])
    results = await asyncio.gather(notify(shop, o1), notify(shop, o2))
    assert sorted(results) == ["duplicate", "paid"]
    assert len(shop.bot.documents) == 1 and yk.refunds == 1


async def test_failed_refund_is_retried(shop, yk):
    o1 = await second_paid_order(shop, yk)
    yk.fail_next("POST /refunds", 500, times=4)
    await notify(shop, o1)
    assert yk.refunds == 0 and any("возврат не прошёл" in t for t in admin_texts(shop))
    await shop.poll_once()
    assert yk.refunds == 1 and (await shop.db.get_payment(o1))["refund_state"] == "requested"
    await shop.poll_once()
    assert yk.refunds == 1                                      # ключ идемпотентности + состояние


async def test_duplicate_without_auto_refund(shop, yk):
    shop.cfg.auto_refund_duplicates = False
    o1 = await second_paid_order(shop, yk)
    assert await notify(shop, o1) == "duplicate"
    assert yk.refunds == 0 and any("Автовозврат выключен" in t for t in admin_texts(shop))
    await shop.poll_once()
    assert yk.refunds == 0


async def test_refund_after_purchase(shop, yk):
    order = await buy_and_pay(shop, yk)
    yk.refund_all(yk.by_order(order)["id"])
    assert await notify(shop, order, "refund.succeeded") == "refunded"
    assert (await shop.db.get_payment(order))["status"] == "refunded"
    assert not await shop.db.has_paid(USER)
    assert (await shop.db.stats())["revenue"] == 0
    assert any("возвращено" in t for t in admin_texts(shop))


# ---------- выдача ----------
async def test_failed_delivery_is_retried(shop, yk, monkeypatch):
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 2
    await buy_and_pay(shop, yk)
    assert shop.bot.documents == []
    await shop.poll_once()
    await shop.poll_once()
    assert len(shop.bot.documents) == 1
    assert len([t for t in user_texts(shop) if "Оплата прошла" in t]) == 1
    assert len([t for t in admin_texts(shop) if "Не удалось выдать" in t]) == 1
    await shop.poll_once()
    assert len(shop.bot.documents) == 1


async def test_retry_waits_for_backoff(shop, yk, monkeypatch):
    shop.bot.fail_documents = 1
    await buy_and_pay(shop, yk)
    await shop.poll_once()
    assert shop.bot.documents == []
    later = dbmod.now() + timedelta(seconds=20)
    monkeypatch.setattr(dbmod, "now", lambda: later)
    await shop.poll_once()
    assert len(shop.bot.documents) == 1


async def test_delivery_gives_up_and_returning_user_still_gets_guide(shop, yk, monkeypatch):
    monkeypatch.setattr("bot.service.retry_delay", lambda attempts: 0)
    shop.bot.fail_documents = 10 ** 6
    order = await buy_and_pay(shop, yk)
    for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
        await shop.poll_once()
    assert (await shop.db.get_payment(order))["delivery_attempts"] == MAX_DELIVERY_ATTEMPTS
    shop.bot.fail_documents = 0
    await shop.poll_once()
    assert shop.bot.documents == []
    assert await shop.deliver_pending_for_user(USER)
    assert len(shop.bot.documents) == 1


async def test_returning_user_gets_single_thanks(shop, yk):
    shop.bot.fail_documents = 1
    await buy_and_pay(shop, yk)
    assert await shop.deliver_pending_for_user(USER)
    assert len(shop.bot.documents) == 1
    assert len([t for t in user_texts(shop) if "Оплата прошла" in t]) == 1


async def test_sale_notification_reminds_about_receipt(shop, yk):
    await buy_and_pay(shop, yk)
    sale = next(t for t in admin_texts(shop) if "Новая продажа" in t)
    assert "Мой налог" in sale and "990.00" in sale


async def test_test_sale_has_no_receipt_reminder(shop, yk):
    shop.cfg.test_mode = yk.test_payments = True
    await buy_and_pay(shop, yk)
    sale = next(t for t in admin_texts(shop) if "Новая продажа" in t)
    assert "Мой налог" not in sale


async def test_restart_during_sending_is_redelivered(shop, yk):
    order = await buy_and_pay(shop, yk)
    await shop.db._write("UPDATE payments SET delivered=? WHERE payment_id=?", (SENDING, order))
    await shop.db.close()
    await shop.db.connect()
    await shop.poll_once()
    assert len(shop.bot.documents) == 2                         # лучше дважды, чем ни разу
    assert (await shop.db.get_payment(order))["delivered"] == 1


async def test_notification_is_acknowledged_without_waiting_for_telegram(shop, yk):
    slow = shop.bot.send_document

    async def slow_send(*a, **kw):
        await asyncio.sleep(1.5)
        return await slow(*a, **kw)
    shop.bot.send_document = slow_send
    order, _ = await shop.get_or_create_payment(USER)
    pid = yk.by_order(order)["id"]
    yk.pay(pid)
    t = time.monotonic()
    assert await shop.handle_notification("payment.succeeded", pid) == "paid"
    assert time.monotonic() - t < 1
    await asyncio.sleep(0.2)                                    # фоновая выдача началась
    assert await shop._deliver(order) == "in_progress"
    await shop.wait_background()
    assert len(shop.bot.documents) == 1


async def test_watermark_keeps_pages_and_bookmarks(shop, yk):
    await buy_and_pay(shop, yk)
    stamped = PdfReader(io.BytesIO(shop.bot.documents[0][1].data))
    original = PdfReader(str(shop.cfg.product_file))
    assert len(stamped.pages) == len(original.pages) and len(stamped.outline) == len(original.outline)
    assert "@ivan" in stamped.pages[-1].extract_text()


async def test_watermark_failure_still_delivers(shop, yk, monkeypatch):
    def broken(*a):
        raise RuntimeError("no font")
    monkeypatch.setattr("bot.service.stamp_pdf", broken)
    await buy_and_pay(shop, yk)
    assert shop.bot.documents[0][1].data == shop.cfg.product_file.read_bytes()
    assert any("водяной знак" in t for t in admin_texts(shop))


async def test_watermarked_copy_is_built_once_and_stale_file_id_rebuilt(shop, monkeypatch):
    import bot.service as service
    calls = []
    real = service.stamp_pdf
    monkeypatch.setattr(service, "stamp_pdf", lambda *a: calls.append(1) or real(*a))
    for _ in range(3):
        await shop.send_product(USER)
    assert len(calls) == 1 and shop.bot.documents[1][1] == "FILEID"
    send = shop.bot.send_document

    async def picky(chat_id, document, **kw):
        if document == "FILEID":
            raise RuntimeError("Bad Request: wrong file identifier/HTTP URL specified")
        return await send(chat_id, document, **kw)
    shop.bot.send_document = picky
    await shop.send_product(USER)
    assert len(calls) == 2


async def test_grant(shop):
    assert await shop.grant(222) == "succeeded"
    assert await shop.db.has_paid(222) and len(shop.bot.documents) == 1
    s = await shop.db.stats()
    assert s["sales"] == 0 and s["manual"] == 1


# ---------- база ----------
async def test_old_prodamus_database_is_migrated(tmp_path):
    import aiosqlite
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as c:
        await c.executescript("""
            CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                                source TEXT, created_at TEXT NOT NULL);
            CREATE TABLE payments (payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount TEXT NOT NULL,
                status TEXT NOT NULL, confirmation_url TEXT, created_at TEXT NOT NULL, paid_at TEXT,
                delivered INTEGER NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 0);
            INSERT INTO payments VALUES ('1793005224-1', 1, '890.00', 'succeeded', NULL,
                '2026-10-20T00:00:00+00:00', '2026-10-20T00:00:00+00:00', 1, 0);
            INSERT INTO payments VALUES ('1793005225-2', 2, '890.00', 'pending', 'https://x.payform.ru',
                '2026-10-20T00:00:00+00:00', NULL, 0, 0);""")
    db = Database(path)
    await db.connect()
    try:
        assert (await db.get_payment("1793005224-1"))["provider_id"] is None
        assert (await db.stats())["sales"] == 1
        assert await db.expire_unsent(23) == 1                  # старый неоплаченный заказ закрыт
    finally:
        await db.close()


async def test_daily_backup(shop):
    await shop.poll_once()
    folder = shop.cfg.db_path.parent / "backups"
    assert len(list(folder.glob("bot-*.db"))) == 1
    for i in range(20):
        (folder / f"bot-2000-01-{i + 1:02d}.db").write_bytes(b"x")
    shop._last_backup = None
    await shop.poll_once()
    assert len(list(folder.glob("bot-*.db"))) == 14


# ---------- веб-адрес уведомлений ----------
def note(event, obj):
    return {"type": "notification", "event": event, "object": obj}


YK_IP = {"X-Forwarded-For": "185.71.76.5"}


async def test_web_notify(shop, yk):
    order, _ = await shop.get_or_create_payment(USER)
    pid = yk.by_order(order)["id"]
    yk.pay(pid)
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.post("/pay/notify", json=note("payment.succeeded", {"id": pid}),
                              headers={"X-Forwarded-For": "6.6.6.6"})
        assert r.status == 403 and shop.bot.documents == []
        spoof = await client.post("/pay/notify", json=note("payment.succeeded", {"id": pid}),
                                  headers={"X-Forwarded-For": "185.71.76.5, 6.6.6.6"})
        assert spoof.status == 403                              # подставленный заголовок не помогает
        assert (await client.post("/pay/notify", data="{oops", headers=YK_IP)).status == 400
        assert (await client.post("/pay/notify", json={"type": "x"}, headers=YK_IP)).status == 400
        bad_id = await client.post("/pay/notify", json=note("payment.succeeded", {"id": "../../etc"}), headers=YK_IP)
        assert bad_id.status == 400
        other = await client.post("/pay/notify", json=note("payout.succeeded", {"id": pid}), headers=YK_IP)
        assert other.status == 200
        ok = await client.post("/pay/notify", json=note("payment.succeeded", {"id": pid, "status": "succeeded"}),
                               headers=YK_IP)
        assert ok.status == 200
        again = await client.post("/pay/notify", json=note("payment.succeeded", {"id": pid}), headers=YK_IP)
        assert again.status == 200
        await shop.wait_background()
        order2, _ = await shop.get_or_create_payment(222)
        yk.fail_next("GET /payments/", 500, times=4)
        down = await client.post("/pay/notify", json=note("payment.succeeded", {"id": yk.by_order(order2)["id"]}),
                                 headers=YK_IP)
        assert down.status == 503                               # ЮKassa повторит позже
        assert (await client.get("/pay/notify")).status == 405
        succ = await client.get("/pay/success", allow_redirects=False)
        assert succ.status == 302 and succ.headers["Location"] == "https://t.me/guide_bot?start=paid"
        assert (await client.get("/health")).status == 200
        big = await client.post("/pay/notify", data="x" * 300_000, headers=YK_IP)
        assert big.status == 413
    assert len(shop.bot.documents) == 1
    assert len([t for t in admin_texts(shop) if "не от ЮKassa" in t]) == 1   # без спама


async def test_web_notify_refund_event(shop, yk):
    order = await buy_and_pay(shop, yk)
    pid = yk.by_order(order)["id"]
    yk.refund_all(pid)
    refund = {"id": "2a0e8b3e-0015-5000-9000-1a2b3c4d5e6f", "payment_id": pid, "status": "succeeded"}
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.post("/pay/notify", json=note("refund.succeeded", refund),
                              headers={"X-Forwarded-For": "77.75.153.78"})
        assert r.status == 200
    assert (await shop.db.get_payment(order))["status"] == "refunded"


async def test_ip_check_can_be_disabled(shop, yk):
    shop.cfg.check_ip = False
    order, _ = await shop.get_or_create_payment(USER)
    pid = yk.by_order(order)["id"]
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.post("/pay/notify", json=note("payment.succeeded", {"id": pid}),
                              headers={"X-Forwarded-For": "6.6.6.6"})
        assert r.status == 200 and shop.bot.documents == []    # API говорит pending — гайда нет


async def test_web_notify_when_payments_disabled(shop):
    shop.cfg.secret_key = ""
    async with TestClient(TestServer(make_app(shop))) as client:
        assert (await client.post("/pay/notify", json={})).status == 503


async def test_offer_page(shop):
    from bot.offer import missing_fields, render_offer
    shop.cfg.seller_name, shop.cfg.seller_inn = "Малютин Максим", "701760448883"
    shop.cfg.seller_email, shop.cfg.support_username = "ivan@example.com", "help_me"
    page = render_offer(shop.cfg, "guide_bot")
    for part in ("Малютин Максим", "ИНН 701760448883", "@guide_bot", "990 рублей", "ЮKassa", "/privacy"):
        assert part in page, part
    assert "Prodamus" not in page and missing_fields(shop.cfg) == []
    async with TestClient(TestServer(make_app(shop))) as client:
        r = await client.get("/offer")
        assert r.status == 200 and r.headers["X-Content-Type-Options"] == "nosniff"
