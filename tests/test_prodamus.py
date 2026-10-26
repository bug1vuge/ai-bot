"""Подпись Prodamus сверена с PHP: tests/data/prodamus_notification.txt разобран функцией parse_str
(так PHP наполняет $_POST) и подписан логикой класса Hmac из документации Prodamus
(strval → рекурсивный ksort → json_encode(JSON_UNESCAPED_UNICODE) → hash_hmac sha256)."""
from pathlib import Path

import pytest

from bot.prodamus import Prodamus, ProdamusError, notification_fields, parse_form, sign, verify

BODY = (Path(__file__).parent / "data" / "prodamus_notification.txt").read_text(encoding="utf-8")
KEY = "secret-KEY-123"
# Получено в PHP 8.3 для BODY и KEY:
PHP_SIGN_DEFAULT = "a5966ca5e74b408ddc408fa177c1b4504fdc0d6475a3c7425bdabde75b4f7456"          # «/» → «\/»
PHP_SIGN_UNESCAPED_SLASHES = "75b490e8edaf5efff27657fee5068d6af0aefadf0c09ef7679f99f5aa632ac0d"  # «/» как есть


def test_parse_form_like_php():
    data = parse_form(BODY)
    assert data["order_num"] == "1793005224-111" and data["payment_status"] == "success"
    assert data["products"][0]["name"] == "Гайд «ИИ», PDF" and data["products"][1]["name"] == "x/y"
    assert data["link"] == "https://aibezlesa.payform.ru/p/abc" and data["empty"] == ""


def test_signature_matches_php():
    data = parse_form(BODY)
    assert sign(data, KEY) == PHP_SIGN_DEFAULT
    assert verify(data, KEY, PHP_SIGN_DEFAULT)
    assert verify(data, KEY, PHP_SIGN_UNESCAPED_SLASHES.upper())


def test_signature_rejects_tampering():
    data = parse_form(BODY)
    assert not verify(data, "other-key", PHP_SIGN_DEFAULT)
    assert not verify({**data, "sum": "1.00"}, KEY, PHP_SIGN_DEFAULT)
    assert not verify(data, KEY, "")
    assert not verify(data, "", PHP_SIGN_DEFAULT)


def test_key_order_does_not_matter():
    data = parse_form(BODY)
    shuffled = dict(reversed(list(data.items())))
    assert verify(shuffled, KEY, PHP_SIGN_DEFAULT)


def test_parse_notification():
    p = Prodamus("https://aibezlesa.payform.ru", KEY)
    data = p.parse_notification(BODY.encode(), "application/x-www-form-urlencoded", PHP_SIGN_DEFAULT)
    f = notification_fields(data)
    assert f == {"order": "1793005224-111", "prodamus_order": "31337", "sum": "890.00", "status": "success",
                 "status_text": "Успешная оплата"}
    for body, sign_, err in [(b"", PHP_SIGN_DEFAULT, "пустое"), (BODY.encode(), "", "нет заголовка"),
                             (BODY.encode(), "ab" * 32, "неверная подпись"), (b"{x", "a", "не JSON")]:
        with pytest.raises(ProdamusError, match=err):
            p.parse_notification(body, "application/json" if body.startswith(b"{") else "", sign_)


def test_nested_submit_variant():
    """В режиме callbackType=json подписывается вложенный объект submit."""
    inner = {"order_num": "5-1", "sum": "890.00", "payment_status": "success"}
    outer = {"submit": inner, "extra": "x"}
    p = Prodamus("https://a.payform.ru", KEY)
    data = p.parse_notification(__import__("json").dumps(outer).encode(), "application/json", sign(inner, KEY))
    assert notification_fields(data)["order"] == "5-1"


def test_payment_url_has_no_secret():
    url = Prodamus("https://a.payform.ru", KEY).payment_url("1-2", "890.00", "Гайд")
    assert KEY not in url and "do=pay" in url
