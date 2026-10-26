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


def test_validate_requires_all_robokassa_fields(sample_pdf):
    cfg = Config()
    cfg.bot_token, cfg.product_file, cfg.public_url = "1:x", sample_pdf, ""
    cfg.rk_login, cfg.rk_password1, cfg.rk_password2 = "shop", "p1", ""
    with pytest.raises(ConfigError, match="все три"):
        cfg.validate()


def test_dollar_in_password_is_not_expanded(tmp_path):
    """set_env.py пишет пароль в .env, бот читает его без подстановки переменных."""
    from deploy.set_env import get_env, set_env
    env = tmp_path / ".env"
    secret = 'a$HOME${USER}"q\\z #x'
    set_env(env, "ROBOKASSA_PASSWORD1", secret)
    assert get_env(env, "ROBOKASSA_PASSWORD1") == secret
    assert dotenv_values(env, interpolate=False)["ROBOKASSA_PASSWORD1"] == secret
