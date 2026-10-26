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


def test_validate_prodamus_fields(sample_pdf):
    cfg = Config()
    cfg.bot_token, cfg.product_file, cfg.public_url = "1:x", sample_pdf, "https://bot.example.ru"
    cfg.pay_url, cfg.pay_secret = "https://aibezlesa.payform.ru", ""
    with pytest.raises(ConfigError, match="оба поля"):
        cfg.validate()
    cfg.pay_secret = "k"
    cfg.validate()
    assert cfg.payments_enabled and cfg.notify_url == "https://bot.example.ru/pay/notify"
    cfg.pay_url = "aibezlesa.payform.ru/"
    with pytest.raises(ConfigError, match="payform"):
        cfg.validate()
    cfg.pay_url, cfg.public_url = "https://aibezlesa.payform.ru", ""
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        cfg.validate()


def test_profile_texts_fit_telegram_limits(sample_pdf):
    from bot.main import profile_texts
    cfg = Config()
    cfg.seller_name, cfg.seller_inn = "Малютин Максим", "701760448883"
    description, short = profile_texts(cfg)
    assert "Малютин Максим" in description and "ИНН 701760448883" in description and "890 ₽" in description
    assert "ИНН 701760448883" in short and "890 ₽" in short
    assert len(description) <= 512 and len(short) <= 120


def test_dollar_in_password_is_not_expanded(tmp_path):
    """set_env.py пишет пароль в .env, бот читает его без подстановки переменных."""
    from deploy.set_env import get_env, set_env
    env = tmp_path / ".env"
    secret = 'a$HOME${USER}"q\\z #x'
    set_env(env, "PRODAMUS_SECRET", secret)
    assert get_env(env, "PRODAMUS_SECRET") == secret
    assert dotenv_values(env, interpolate=False)["PRODAMUS_SECRET"] == secret
