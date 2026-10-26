"""Настройки бота. Все значения берутся из переменных окружения (файл .env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# interpolate=False: пароли с «$» (например, «a$b» или «${x}») читаются как есть
load_dotenv(BASE_DIR / ".env", interpolate=False)


class ConfigError(SystemExit):
    """Понятная ошибка настройки вместо трассировки Python."""


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} в .env должно быть целым числом, сейчас: «{raw}»") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} в .env должно быть не меньше {minimum}, сейчас: {value}")
    return value


def _int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    items = [x for x in raw.replace(" ", "").split(",") if x]
    bad = [x for x in items if not x.lstrip("-").isdigit()]
    if bad:
        raise ConfigError(f"{name} в .env — числовые Telegram ID через запятую, непонятно: {', '.join(bad)}")
    return [int(x) for x in items]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN"))

    # ---- Робокасса ----
    rk_login: str = field(default_factory=lambda: _env("ROBOKASSA_LOGIN"))
    rk_password1: str = field(default_factory=lambda: _env("ROBOKASSA_PASSWORD1"))
    rk_password2: str = field(default_factory=lambda: _env("ROBOKASSA_PASSWORD2"))
    rk_test: bool = field(default_factory=lambda: _bool("ROBOKASSA_TEST", True))
    # В тестовом режиме гайд выдаётся без настоящей оплаты, поэтому покупать могут только
    # администраторы. true — открыть тестовую покупку всем (например, для проверки модератором).
    rk_test_public: bool = field(default_factory=lambda: _bool("ROBOKASSA_TEST_PUBLIC", False))
    rk_hash: str = field(default_factory=lambda: _env("ROBOKASSA_HASH", "md5").lower())
    rk_send_receipt: bool = field(default_factory=lambda: _bool("ROBOKASSA_SEND_RECEIPT", False))
    receipt_item_name: str = field(default_factory=lambda: _env("RECEIPT_ITEM_NAME", "Электронное руководство ИИ без тёмного леса, PDF"))

    # ---- товар ----
    price_rub: int = field(default_factory=lambda: _int("PRICE_RUB", 890, minimum=1))
    product_title: str = field(default_factory=lambda: _env("PRODUCT_TITLE", "Гайд «ИИ без тёмного леса»"))
    payment_description: str = field(default_factory=lambda: _env("PAYMENT_DESCRIPTION", "Гайд ИИ без тёмного леса, PDF"))
    product_file: Path = field(default_factory=lambda: BASE_DIR / _env("PRODUCT_FILE", "data/guide.pdf"))
    product_filename: str = field(default_factory=lambda: _env("PRODUCT_FILENAME", "ИИ без тёмного леса — гайд 2026.pdf"))
    watermark: bool = field(default_factory=lambda: _bool("WATERMARK", True))

    # ---- контакты ----
    admin_ids: list[int] = field(default_factory=lambda: _int_list("ADMIN_IDS"))
    offer_url: str = field(default_factory=lambda: _env("OFFER_URL"))
    support_username: str = field(default_factory=lambda: _env("SUPPORT_USERNAME").lstrip("@"))
    # Реквизиты продавца — подставляются в оферту и в раздел «Поддержка»
    seller_name: str = field(default_factory=lambda: _env("SELLER_NAME"))
    seller_inn: str = field(default_factory=lambda: _env("SELLER_INN"))
    seller_email: str = field(default_factory=lambda: _env("SELLER_EMAIL"))
    seller_info: str = field(default_factory=lambda: _env("SELLER_INFO"))

    # ---- веб-сервер для уведомлений Робокассы ----
    web_enabled: bool = field(default_factory=lambda: _bool("WEB_ENABLED", True))
    web_host: str = field(default_factory=lambda: _env("WEB_HOST", "127.0.0.1"))
    web_port: int = field(default_factory=lambda: _int("WEB_PORT", 8080, minimum=1))
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL").rstrip("/"))

    # ---- связь с Telegram ----
    # Примеры: socks5://user:pass@1.2.3.4:1080  или  http://127.0.0.1:10809
    telegram_proxy: str = field(default_factory=lambda: _env("TELEGRAM_PROXY"))

    # ---- техническое ----
    poll_interval_sec: int = field(default_factory=lambda: _int("POLL_INTERVAL_SEC", 30, minimum=5))
    payment_ttl_min: int = field(default_factory=lambda: _int("PAYMENT_TTL_MIN", 60, minimum=5))
    db_path: Path = field(default_factory=lambda: BASE_DIR / _env("DB_PATH", "data/bot.db"))

    def __post_init__(self) -> None:
        # своя страница оферты на сервере бота, если ссылка не задана вручную
        if not self.offer_url and self.public_url:
            self.offer_url = f"{self.public_url}/offer"
        if not self.seller_info and self.seller_name:
            self.seller_info = f"{self.seller_name}, самозанятый" + (f", ИНН {self.seller_inn}" if self.seller_inn else "")

    @property
    def payments_enabled(self) -> bool:
        """Без данных Робокассы бот работает в режиме витрины: всё показывает, но оплату не принимает."""
        return bool(self.rk_login and self.rk_password1 and self.rk_password2)

    def can_buy(self, user_id: int) -> bool:
        """Можно ли этому пользователю выдать ссылку на оплату."""
        if not self.payments_enabled:
            return False
        return not self.rk_test or self.rk_test_public or user_id in self.admin_ids

    def validate(self) -> None:
        if not self.bot_token:
            raise ConfigError("Не заполнен BOT_TOKEN в файле .env")
        filled = [bool(self.rk_login), bool(self.rk_password1), bool(self.rk_password2)]
        if any(filled) and not all(filled):
            raise ConfigError("Заполните все три поля Робокассы (ROBOKASSA_LOGIN, ROBOKASSA_PASSWORD1, "
                             "ROBOKASSA_PASSWORD2) или оставьте все пустыми")
        if self.rk_hash not in {"md5", "sha1", "sha256", "sha384", "sha512"}:
            raise ConfigError("ROBOKASSA_HASH должен быть одним из: md5, sha1, sha256, sha384, sha512")
        if not 1 <= self.web_port <= 65535:
            raise ConfigError(f"WEB_PORT должен быть от 1 до 65535, сейчас: {self.web_port}")
        if self.public_url and not self.public_url.startswith("https://"):
            raise ConfigError("PUBLIC_URL должен начинаться с https:// — Робокасса работает только с HTTPS")
        if not self.product_file.exists():
            raise ConfigError(f"Не найден файл продукта: {self.product_file}. Положите PDF в папку data/.")

    @property
    def price_str(self) -> str:
        return f"{self.price_rub:.2f}"
