"""Клиент API ЮKassa: адреса уведомлений, разбор ответов, правила повторов."""
from decimal import Decimal

import pytest

from bot.yookassa import Payment, YooKassa, YooKassaError, is_yookassa_ip
from tests.fake_yookassa import SECRET, SHOP_ID


@pytest.mark.parametrize("ip, ok", [
    ("185.71.76.1", True), ("185.71.77.31", True), ("77.75.153.78", True), ("77.75.156.11", True),
    ("77.75.156.35", True), ("77.75.154.200", True), ("2a02:5180::1", True), ("::ffff:185.71.76.5", True),
    ("185.71.76.32", False), ("77.75.156.12", False), ("127.0.0.1", False), ("10.0.0.1", False),
    ("", False), ("not-an-ip", False), ("185.71.76.1, 6.6.6.6", False),
])
def test_yookassa_ip_ranges(ip, ok):
    assert is_yookassa_ip(ip) is ok


def test_payment_parse_is_defensive():
    p = Payment.parse({"id": "x", "status": "succeeded", "paid": "true", "amount": {"value": "abc"},
                       "metadata": ["not", "a", "dict"], "test": "false"})
    assert p.paid is False and p.amount is None and p.metadata == {} and p.test is False
    assert p.refunded == Decimal("0.00") and p.confirmation_url == ""
    with pytest.raises(YooKassaError):
        Payment.parse({"status": "succeeded"})
    with pytest.raises(YooKassaError):
        Payment.parse(["not", "a", "dict"])


async def test_invalid_payment_id_is_not_requested(yk):
    client = YooKassa(SHOP_ID, SECRET, yk.url)
    try:
        for bad in ("../payments", "", "1 OR 1=1", "a" * 36):
            with pytest.raises(YooKassaError):
                await client.get_payment(bad)
        assert yk.calls == []
    finally:
        await client.close()


async def test_post_without_idempotence_key_is_never_retried(yk):
    client = YooKassa(SHOP_ID, SECRET, yk.url, backoff=0.01)
    try:
        yk.fail_next("POST /payments", 500, times=3)
        with pytest.raises(YooKassaError):
            await client._request("POST", "/payments", json={"amount": {}})
        assert yk.count("POST /payments") == 1
    finally:
        await client.close()


async def test_unreachable_api_is_temporary_error():
    client = YooKassa(SHOP_ID, SECRET, "http://127.0.0.1:9/v3", timeout=1, attempts=2, backoff=0.01)
    try:
        with pytest.raises(YooKassaError) as e:
            await client.get_payment("2a0e8b3e-000f-5000-9000-1a2b3c4d5e6f")
        assert e.value.temporary and e.value.status == 0
    finally:
        await client.close()


async def test_create_capture_refund_roundtrip(yk):
    client = YooKassa(SHOP_ID, SECRET, yk.url, backoff=0.01)
    try:
        p = await client.create_payment(order_id="1800000000-5", user_id=5, amount="990.00", description="d" * 300,
                                        return_url="https://x.ru/pay/success", idempotence_key="k" * 100)
        assert p.status == "pending" and p.confirmation_url and p.account_id == SHOP_ID
        assert len(yk.calls[-1][2]["description"]) == 128 and list(yk.idempotent) == ["k" * 64]
        yk.wait_capture(p.id)
        captured = await client.capture(await client.get_payment(p.id))
        assert captured.status == "succeeded"
        await client.refund(captured, "test")
        await client.refund(captured, "test")                  # тот же ключ — второго возврата нет
        assert yk.refunds == 1 and (await client.get_payment(p.id)).refunded == Decimal("990.00")
    finally:
        await client.close()
