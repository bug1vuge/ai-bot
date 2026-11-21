"""Проверка разбора .env: понятные ошибки вместо трассировки и чтение паролей как есть."""
import pytest
from dotenv import dotenv_values

from bot.config import Config, ConfigError


def test_invalid_numbers_give_clear_error(monkeypatch):
    monkeypatch.setenv("PRICE_RUB", "890р")
    with pytest.raises(ConfigError, match="PRICE_RUB"):
        Config()
    monkeypatch.setenv("PRICE_RUB", "0")
    with pytest.raises(ConfigError, match="не меньше 1"):
        Config()


def test_admin_ids_validation(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "123, 456")
    assert Config().admin_ids == [123, 456]
    monkeypatch.setenv("ADMIN_IDS", "@me")
    with pytest.raises(ConfigError, match="ADMIN_IDS"):
        Config()


def test_validate_rejects_plain_http(monkeypatch, sample_pdf):
    monkeypatch.setenv("PUBLIC_URL", "http://1-2-3-4.sslip.io")
    cfg = Config()
    cfg.bot_token, cfg.product_file = "1:x", sample_pdf
    with pytest.raises(ConfigError, match="https"):
        cfg.validate()


def test_validate_yookassa_fields(sample_pdf):
    cfg = Config()
    cfg.bot_token, cfg.product_file, cfg.public_url = "1:x", sample_pdf, "https://bot.example.ru"
    cfg.shop_id, cfg.secret_key, cfg.test_mode = "123456", "", False
    cfg.validate()                     # неполные данные — бот запускается в режиме витрины
    assert not cfg.payments_enabled and cfg.showcase_reason == "не заполнено в .env: YOOKASSA_SECRET_KEY"
    cfg.secret_key = "live_" + "a" * 40
    cfg.validate()
    assert cfg.payments_enabled and cfg.notify_url == "https://bot.example.ru/pay/notify"
    assert cfg.return_url == "https://bot.example.ru/pay/success"
    for field, value, error in [("shop_id", "abc", "YOOKASSA_SHOP_ID"),
                                ("secret_key", "my-secret", "YOOKASSA_SECRET_KEY"),
                                ("secret_key", "test_" + "a" * 40, "PAYMENTS_TEST_MODE"),
                                ("api_url", "http://evil.example/v3", "https"),
                                ("public_url", "", "PUBLIC_URL")]:
        broken = Config()
        broken.bot_token, broken.product_file, broken.public_url = "1:x", sample_pdf, "https://bot.example.ru"
        broken.shop_id, broken.secret_key, broken.test_mode = "123456", "live_" + "a" * 40, False
        setattr(broken, field, value)
        with pytest.raises(ConfigError, match=error):
            broken.validate()
    cfg.secret_key, cfg.test_mode = "test_" + "a" * 40, True   # тестовый ключ — только в тестовом режиме
    cfg.validate()


def test_can_buy(sample_pdf):
    cfg = Config()
    cfg.shop_id, cfg.secret_key, cfg.public_url, cfg.admin_ids = "1", "live_" + "a" * 40, "https://x.ru", [7]
    cfg.test_mode = False
    assert cfg.can_buy(1) and cfg.can_buy(7)
    cfg.test_mode = True
    assert not cfg.can_buy(1) and cfg.can_buy(7)
    cfg.secret_key = ""
    assert not cfg.can_buy(7)


def test_profile_texts_fit_telegram_limits(sample_pdf):
    from bot.main import profile_texts
    cfg = Config()
    cfg.seller_name, cfg.seller_inn = "Малютин Максим", "701760448883"
    description, short = profile_texts(cfg)
    assert "Малютин Максим" in description and "ИНН 701760448883" in description and "990 ₽" in description
    assert "ИНН 701760448883" in short and "990 ₽" in short
    assert len(description) <= 512 and len(short) <= 120


def test_dollar_in_password_is_not_expanded(tmp_path):
    """set_env.py пишет пароль в .env, бот читает его без подстановки переменных."""
    from deploy.set_env import get_env, set_env
    env = tmp_path / ".env"
    secret = 'a$HOME${USER}"q\\z #x'
    set_env(env, "YOOKASSA_SECRET_KEY", secret)
    assert get_env(env, "YOOKASSA_SECRET_KEY") == secret
    assert dotenv_values(env, interpolate=False)["YOOKASSA_SECRET_KEY"] == secret
