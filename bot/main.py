"""Точка входа: python -m bot.main"""
import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

from .config import Config
from .db import Database
from .handlers import router
from .middlewares import ThrottleMiddleware
from .offer import missing_fields
from .robokassa import Robokassa
from .service import Shop
from .web import make_app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    cfg.validate()

    db = Database(cfg.db_path)
    await db.connect()
    kassa = Robokassa(cfg.rk_login, cfg.rk_password1, cfg.rk_password2, is_test=cfg.rk_test, algo=cfg.rk_hash)
    session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else AiohttpSession()
    bot = Bot(cfg.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    shop = Shop(cfg, db, kassa, bot)
    try:
        me = await bot.get_me()
    except Exception as e:
        logging.error(
            "Не удалось связаться с Telegram: %s\n"
            "Проверьте токен и доступ к api.telegram.org. Если Telegram отсюда не открывается напрямую — "
            "включите VPN для всего устройства или укажите TELEGRAM_PROXY в .env.", e)
        await bot.session.close()
        await db.close()
        raise SystemExit(1) from None
    shop.bot_username = me.username or ""

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="my", description="Мои покупки"),
        BotCommand(command="help", description="Поддержка"),
    ])

    dp = Dispatcher()
    throttle = ThrottleMiddleware()
    dp.message.outer_middleware(throttle)
    dp.callback_query.outer_middleware(throttle)
    dp.include_router(router)
    dp["shop"] = shop

    runner = None
    if cfg.web_enabled:
        runner = web.AppRunner(make_app(shop))
        await runner.setup()
        await web.TCPSite(runner, cfg.web_host, cfg.web_port).start()
        logging.info("Адреса для Робокассы слушаются на %s:%s", cfg.web_host, cfg.web_port)
        if cfg.public_url:
            logging.info("Result URL:  %s/robokassa/result  (POST)", cfg.public_url)
            logging.info("Success URL: %s/robokassa/success (GET)", cfg.public_url)
            logging.info("Fail URL:    %s/robokassa/fail    (GET)", cfg.public_url)
            logging.info("Оферта:      %s", cfg.offer_url)

    if missing_fields(cfg):
        logging.warning("В оферте не заполнены поля: %s — впишите их в .env", ", ".join(missing_fields(cfg)))
    # фоновая проверка нужна всегда: она же повторяет выдачу, если файл не отправился
    poller = asyncio.create_task(shop.poll_pending())
    logging.info("Бот @%s запущен. Цена %s ₽, водяной знак: %s", shop.bot_username, cfg.price_rub, cfg.watermark)
    if cfg.payments_enabled:
        logging.info("Робокасса: магазин %s, режим %s", cfg.rk_login, "ТЕСТОВЫЙ" if cfg.rk_test else "боевой")
    else:
        logging.warning("Данные Робокассы не указаны — режим витрины: бот всё показывает, но оплату пока не принимает.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        if runner:
            await runner.cleanup()
        await kassa.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
