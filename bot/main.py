"""Точка входа: python -m bot.main (или python -m bot)."""
import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand
from aiohttp import web

from . import texts
from .config import Config, ConfigError
from .db import Database
from .handlers import router
from .middlewares import ThrottleMiddleware
from .offer import missing_fields
from .robokassa import Robokassa
from .service import Shop
from .web import make_app

log = logging.getLogger("bot")

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="my", description="Мои покупки"),
    BotCommand(command="help", description="Поддержка"),
]


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # каждое обновление Telegram в журнале не нужно — только ошибки
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def connect_telegram(bot: Bot) -> str:
    """Ждёт, пока Telegram станет доступен. Пока ждём, адреса для Робокассы уже работают:
    оплата фиксируется, а гайд уйдёт, как только появится связь."""
    delay = 5
    while True:
        try:
            me = await bot.get_me()
            return me.username or ""
        except TelegramUnauthorizedError:
            raise ConfigError("Telegram отклонил BOT_TOKEN — проверьте токен в .env (его выдаёт @BotFather)") from None
        except Exception as e:
            log.error("Нет связи с Telegram: %s. Повтор через %s с. Если Telegram отсюда не открывается напрямую — "
                      "укажите TELEGRAM_PROXY в .env.", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def run(cfg: Config, session: BaseSession | None = None) -> None:
    cfg.validate()
    db = Database(cfg.db_path)
    await db.connect()
    kassa = Robokassa(cfg.rk_login, cfg.rk_password1, cfg.rk_password2, is_test=cfg.rk_test, algo=cfg.rk_hash)
    if session is None:
        session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else AiohttpSession()
    bot = Bot(cfg.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    shop = Shop(cfg, db, kassa, bot)

    dp = Dispatcher()
    throttle = ThrottleMiddleware()
    dp.message.outer_middleware(throttle)
    dp.callback_query.outer_middleware(throttle)
    dp.include_router(router)
    dp["shop"] = shop

    runner: web.AppRunner | None = None
    poller: asyncio.Task | None = None
    try:
        # 1. Адреса для Робокассы — первыми, независимо от доступности Telegram
        if cfg.web_enabled:
            runner = web.AppRunner(make_app(shop), access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, cfg.web_host, cfg.web_port).start()
            except OSError as e:
                raise ConfigError(f"Не удалось занять порт {cfg.web_host}:{cfg.web_port} ({e}). "
                                  "Его использует другая программа — укажите другой WEB_PORT в .env.") from None
            log.info("Адреса для Робокассы слушаются на %s:%s", cfg.web_host, cfg.web_port)
            if cfg.public_url:
                log.info("Result URL:  %s/robokassa/result  (POST)", cfg.public_url)
                log.info("Success URL: %s/robokassa/success (GET)", cfg.public_url)
                log.info("Fail URL:    %s/robokassa/fail    (GET)", cfg.public_url)
                log.info("Оферта:      %s", cfg.offer_url)

        # 2. Фоновая проверка оплат и повторная выдача
        poller = asyncio.create_task(shop.poll_pending(), name="poller")

        # 3. Telegram
        shop.bot_username = await connect_telegram(bot)
        try:
            await bot.set_my_commands(COMMANDS)
        except Exception as e:
            log.warning("Не удалось обновить меню команд: %s", e)

        if missing_fields(cfg):
            log.warning("В оферте не заполнены поля: %s — впишите их в .env", ", ".join(missing_fields(cfg)))
        log.info("Бот @%s запущен. Цена %s ₽, водяной знак: %s", shop.bot_username, cfg.price_rub, cfg.watermark)
        if cfg.payments_enabled:
            mode = "ТЕСТОВЫЙ" if cfg.rk_test else "боевой"
            log.info("Робокасса: магазин %s, режим %s", cfg.rk_login, mode)
            if cfg.rk_test:
                log.warning("Тестовый режим Робокассы: деньги не списываются, а гайд выдаётся!")
            await shop._notify_admins(texts.ADMIN_STARTED.format(mode=mode)
                                      + (texts.TEST_MODE_WARNING if cfg.rk_test else ""))
        else:
            log.warning("Данные Робокассы не указаны — режим витрины: бот всё показывает, но оплату пока не принимает.")
            await shop._notify_admins(texts.ADMIN_STARTED_SHOWCASE)

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Остановка бота…")
        if poller:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
        await shop.wait_background()
        if runner:
            await runner.cleanup()
        await kassa.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run(Config()))
    except ConfigError as e:
        log.error("%s", e.code)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
