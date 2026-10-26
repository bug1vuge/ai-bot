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
from .prodamus import Prodamus
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
    """Ждёт, пока Telegram станет доступен. Пока ждём, приём уведомлений об оплате уже работает:
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


def profile_texts(cfg: Config) -> tuple[str, str]:
    """Описание бота (видно до нажатия «Старт») и короткое описание (в профиле бота).
    Здесь же реквизиты продавца и цена — их требует проверка платёжной системы."""
    price = f"{cfg.price_rub:,}".replace(",", " ")
    seller = ""
    if cfg.seller_name:
        seller = f"Продавец: {cfg.seller_name}, самозанятый" + (f", ИНН {cfg.seller_inn}" if cfg.seller_inn else "")
    description = texts.BOT_DESCRIPTION.format(price=price) + (f"\n\n{seller}" if seller else "")
    short = texts.BOT_SHORT_DESCRIPTION.format(price=price)
    if cfg.seller_name and cfg.seller_inn:
        short += f" {cfg.seller_name}, ИНН {cfg.seller_inn}"
    return description[:512], short[:120]


async def setup_profile(bot: Bot, cfg: Config) -> None:
    """Меню команд и описание бота. Ошибка здесь не мешает работе бота."""
    description, short = profile_texts(cfg)
    for name, call in (("меню команд", bot.set_my_commands(COMMANDS)),
                       ("описание", bot.set_my_description(description)),
                       ("короткое описание", bot.set_my_short_description(short))):
        try:
            await call
        except Exception as e:
            log.warning("Не удалось обновить %s бота: %s", name, e)


async def run(cfg: Config, session: BaseSession | None = None) -> None:
    cfg.validate()
    db = Database(cfg.db_path)
    await db.connect()
    pay = Prodamus(cfg.pay_url, cfg.pay_secret)
    if session is None:
        session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else AiohttpSession()
    bot = Bot(cfg.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    shop = Shop(cfg, db, pay, bot)

    dp = Dispatcher()
    throttle = ThrottleMiddleware()
    dp.message.outer_middleware(throttle)
    dp.callback_query.outer_middleware(throttle)
    dp.include_router(router)
    dp["shop"] = shop

    runner: web.AppRunner | None = None
    poller: asyncio.Task | None = None
    try:
        # 1. Приём уведомлений об оплате — первым, независимо от доступности Telegram
        if cfg.web_enabled:
            runner = web.AppRunner(make_app(shop), access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, cfg.web_host, cfg.web_port).start()
            except OSError as e:
                raise ConfigError(f"Не удалось занять порт {cfg.web_host}:{cfg.web_port} ({e}). "
                                  "Его использует другая программа — укажите другой WEB_PORT в .env.") from None
            log.info("Веб-сервер слушает %s:%s", cfg.web_host, cfg.web_port)
            if cfg.public_url:
                log.info("Адрес для уведомлений Prodamus: %s", cfg.notify_url)
                log.info("Оферта: %s", cfg.offer_url)

        # 2. Фоновая повторная выдача и резервные копии
        poller = asyncio.create_task(shop.poll_pending(), name="poller")

        # 3. Telegram
        shop.bot_username = await connect_telegram(bot)
        await setup_profile(bot, cfg)

        if missing_fields(cfg):
            log.warning("В оферте не заполнены поля: %s — впишите их в .env", ", ".join(missing_fields(cfg)))
        log.info("Бот @%s запущен. Цена %s ₽, водяной знак: %s", shop.bot_username, cfg.price_rub, cfg.watermark)
        if cfg.payments_enabled:
            mode = "тестовый (покупают только администраторы)" if cfg.test_mode else "боевой"
            log.info("Prodamus: %s, режим %s", cfg.pay_url, mode)
            await shop._notify_admins(texts.ADMIN_STARTED.format(mode=mode, notify_url=cfg.notify_url)
                                      + (texts.TEST_MODE_WARNING if cfg.test_mode else ""))
        else:
            log.warning("Режим витрины (%s): бот всё показывает, но оплату пока не принимает.", cfg.showcase_reason)
            await shop._notify_admins(texts.ADMIN_STARTED_SHOWCASE.format(reason=cfg.showcase_reason))

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
