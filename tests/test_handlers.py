"""Сквозной тест обработчиков: /start → «Купить» → оплата → «Я оплатил» — через настоящий Dispatcher aiogram."""
import datetime as dt

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import Chat, Document, Message, Update, User

from bot.db import Database
from bot.handlers import router
from bot.service import Shop
from tests.test_shop import FakeKassa, make_cfg

USER = User(id=111, is_bot=False, first_name="Иван", username="ivan")
CHAT = Chat(id=111, type="private")


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(message_id=len(self.calls), date=dt.datetime.now(), chat=CHAT,
                           text=getattr(method, "text", None),
                           document=Document(file_id="F", file_unique_id="U") if isinstance(method, SendDocument) else None)
        return True

    async def stream_content(self, *a, **kw):
        yield b""

    async def close(self):
        pass


@pytest.fixture
async def env(tmp_path, sample_pdf):
    cfg = make_cfg(tmp_path, sample_pdf)
    cfg.admin_ids = []
    cfg.seller_info = "Иванов Иван, самозанятый, ИНН 123456789012"
    cfg.offer_url = "https://example.com/offer"
    cfg.support_username = "help_me"
    cfg.watermark = False
    db = Database(cfg.db_path)
    await db.connect()
    session = RecordingSession()
    bot = Bot("123456:" + "A" * 35, session=session)
    shop = Shop(cfg, db, FakeKassa(), bot)
    shop.bot_username = "guide_bot"
    dp = Dispatcher()
    dp.include_router(router)
    dp["shop"] = shop
    yield dp, bot, shop, session
    router._parent_router = None  # отцепляем общий роутер для следующего теста
    await db.close()


def msg_update(uid, text):
    return Update(update_id=uid, message=Message(message_id=uid, date=dt.datetime.now(), chat=CHAT, from_user=USER, text=text))


def cb_update(uid, data):
    from aiogram.types import CallbackQuery
    m = Message(message_id=1, date=dt.datetime.now(), chat=CHAT, text="x")
    return Update(update_id=uid, callback_query=CallbackQuery(id=str(uid), from_user=USER, chat_instance="c", message=m, data=data))


def texts_of(session):
    return [c.text for c in session.calls if isinstance(c, SendMessage)]


async def test_full_purchase_flow(env):
    dp, bot, shop, session = env
    await dp.feed_update(bot, msg_update(1, "/start tiktok"))
    assert "ИИ без тёмного леса" in texts_of(session)[-1]
    assert (await shop.db.get_user(111))["source"] == "tiktok"

    await dp.feed_update(bot, cb_update(2, "buy"))
    pay_msg = [c for c in session.calls if isinstance(c, SendMessage)][-1]
    assert "оферты" in pay_msg.text
    button_url = pay_msg.reply_markup.inline_keyboard[0][0].url
    assert button_url.startswith("https://auth.robokassa.ru/Merchant/Index.aspx?") and "IsTest=1" in button_url
    pid = pay_msg.reply_markup.inline_keyboard[1][0].callback_data.split(":")[1]

    await dp.feed_update(bot, cb_update(3, f"check:{pid}"))
    assert "пока не поступила" in texts_of(session)[-1]

    shop.kassa.states[pid] = {"result": 0, "state": 100, "out_sum": "890.00"}
    await dp.feed_update(bot, msg_update(4, "/start paid"))   # вернулся со страницы оплаты (Success URL)
    assert any("Оплата прошла" in t for t in texts_of(session))
    assert sum(isinstance(c, SendDocument) for c in session.calls) == 1

    await dp.feed_update(bot, msg_update(5, "/my"))
    assert sum(isinstance(c, SendDocument) for c in session.calls) == 2
    # источник не перезаписывается повторным /start
    assert (await shop.db.get_user(111))["source"] == "tiktok"


async def test_admin_commands_hidden_for_users(env):
    dp, bot, shop, session = env
    before = len(session.calls)
    await dp.feed_update(bot, msg_update(10, "/stats"))
    await dp.feed_update(bot, msg_update(11, "/grant 5"))
    assert len(session.calls) == before  # обычный пользователь ничего не получает


async def test_admin_stats(env):
    dp, bot, shop, session = env
    shop.cfg.admin_ids = [111]
    await dp.feed_update(bot, msg_update(20, "/start vk"))
    await dp.feed_update(bot, msg_update(21, "/stats"))
    assert "Статистика" in texts_of(session)[-1]


async def test_showcase_mode_without_keys(env):
    """Без данных Робокассы бот показывает экран оплаты, но счёт не создаёт."""
    dp, bot, shop, session = env
    shop.cfg.rk_login = shop.cfg.rk_password1 = shop.cfg.rk_password2 = ""
    await dp.feed_update(bot, msg_update(30, "/start"))
    await dp.feed_update(bot, cb_update(31, "buy"))
    pay_msg = [c for c in session.calls if isinstance(c, SendMessage)][-1]
    assert "890" in pay_msg.text and "оферты" in pay_msg.text
    assert pay_msg.reply_markup.inline_keyboard[0][0].callback_data == "soon"
    assert await shop.db.latest_pending(111) is None
    await dp.feed_update(bot, cb_update(32, "soon"))
    from aiogram.methods import AnswerCallbackQuery
    alert = [c for c in session.calls if isinstance(c, AnswerCallbackQuery)][-1]
    assert alert.show_alert and "подключается" in alert.text


async def test_support_shows_seller_and_offer(env):
    dp, bot, shop, session = env
    await dp.feed_update(bot, msg_update(40, "/help"))
    t = texts_of(session)[-1]
    assert "@help_me" in t and "ИНН 123456789012" in t and "оферты" in t


async def test_fail_return(env):
    dp, bot, shop, session = env
    await dp.feed_update(bot, msg_update(50, "/start fail"))
    assert "Оплата не завершена" in texts_of(session)[-1]
    assert (await shop.db.get_user(111))["source"] is None
