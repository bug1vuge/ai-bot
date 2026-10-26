"""Обработчики сообщений и кнопок."""
import contextlib
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import texts
from .service import Shop

log = logging.getLogger(__name__)
router = Router()
# Бот работает только в личных сообщениях: в группах и каналах он молчит
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

# Повторная отправка файла не чаще, чем раз в столько секунд на пользователя
RESEND_COOLDOWN_SEC = 20
_last_sent: dict[int, float] = {}


def _price(shop: Shop) -> str:
    return f"{shop.cfg.price_rub:,}".replace(",", " ")


def main_kb(shop: Shop, paid: bool) -> InlineKeyboardMarkup:
    rows = []
    if paid:
        rows.append([InlineKeyboardButton(text=texts.BTN_MY, callback_data="my")])
    else:
        rows.append([InlineKeyboardButton(text=texts.BTN_BUY.format(price=_price(shop)), callback_data="buy")])
        rows.append([InlineKeyboardButton(text=texts.BTN_ABOUT, callback_data="about")])
    rows.append([InlineKeyboardButton(text=texts.BTN_SUPPORT, callback_data="support")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_kb(shop: Shop, url: str, payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_PAY.format(price=_price(shop)), url=url)],
        [InlineKeyboardButton(text=texts.BTN_CHECK, callback_data=f"check:{payment_id}")],
    ])


def _offer_note(shop: Shop) -> str:
    return texts.OFFER_NOTE.format(url=html.escape(shop.cfg.offer_url, quote=True)) if shop.cfg.offer_url else ""


async def _send(bot: Bot, user_id: int, text: str, **kw) -> None:
    """Ответ пользователю в личку. Не зависит от сообщения с кнопкой: оно могло быть удалено
    или стать недоступным (кнопки старше 48 часов)."""
    await bot.send_message(user_id, text, disable_web_page_preview=True, **kw)


async def _send_product(bot: Bot, shop: Shop, user_id: int) -> None:
    """Отправка гайда по запросу пользователя — с защитой от многократных нажатий."""
    now = time.monotonic()
    if now - _last_sent.get(user_id, -RESEND_COOLDOWN_SEC) < RESEND_COOLDOWN_SEC:
        await _send(bot, user_id, texts.RESEND_COOLDOWN)
        return
    _last_sent[user_id] = now
    if len(_last_sent) > 10_000:  # не держим в памяти старые отметки
        for uid, t in list(_last_sent.items()):
            if now - t >= RESEND_COOLDOWN_SEC:
                del _last_sent[uid]
    await shop.send_product(user_id)


# ---------- /start ----------
@router.message(CommandStart())
async def start(message: Message, command: CommandObject, shop: Shop, bot: Bot):
    user = message.from_user
    payload = (command.args or "").strip()[:64] or None
    source = payload if payload and payload not in {"paid", "fail"} else None
    await shop.db.upsert_user(user.id, user.username, user.first_name, source)
    paid = await shop.db.has_paid(user.id)

    # Пользователь вернулся со страницы оплаты (Success URL / Fail URL Робокассы)
    if payload == "paid":
        if paid:
            # гайд уже пришёл по уведомлению Робокассы — не дублируем файл
            await message.answer(texts.PAID_ALREADY_SENT, reply_markup=main_kb(shop, True))
            return
        pending = await shop.db.latest_pending(user.id)
        if pending:
            await _check_and_reply(bot, shop, user.id, pending["payment_id"])
            return
    if payload == "fail" and not paid:
        await message.answer(texts.PAY_FAILED, reply_markup=main_kb(shop, False))
        return

    if paid:
        await message.answer(texts.ALREADY_BOUGHT, reply_markup=main_kb(shop, True))
        return
    name = html.escape(user.first_name or "друг")
    await message.answer(texts.WELCOME.format(name=name, price=_price(shop)), reply_markup=main_kb(shop, False))


# ---------- покупка ----------
@router.callback_query(F.data == "buy")
async def buy(call: CallbackQuery, shop: Shop, bot: Bot):
    await call.answer()
    user_id = call.from_user.id
    if await shop.db.has_paid(user_id):
        await _send(bot, user_id, texts.ALREADY_BOUGHT, reply_markup=main_kb(shop, True))
        return
    if not shop.cfg.payments_enabled:
        # Режим витрины: тот же экран оплаты, но кнопка пока не ведёт в кассу
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_PAY.format(price=_price(shop)), callback_data="soon")]])
        await _send(bot, user_id, _pay_text(shop), reply_markup=kb)
        return
    try:
        payment_id, url = await shop.get_or_create_payment(user_id)
    except Exception:
        log.exception("Не удалось создать платёж для %s", user_id)
        await _send(bot, user_id, texts.CREATE_ERROR)
        return
    await _send(bot, user_id, _pay_text(shop), reply_markup=pay_kb(shop, url, payment_id))


def _pay_text(shop: Shop) -> str:
    return texts.PAY.format(price=_price(shop)) + _offer_note(shop)


@router.callback_query(F.data == "soon")
async def soon(call: CallbackQuery):
    await call.answer(texts.PAYMENTS_SOON, show_alert=True)


@router.callback_query(F.data.startswith("check:"))
async def check(call: CallbackQuery, shop: Shop, bot: Bot):
    await call.answer("Проверяю оплату…")
    payment_id = call.data.split(":", 1)[1]
    row = await shop.db.get_payment(payment_id)
    if row is None or row["user_id"] != call.from_user.id:
        await _send(bot, call.from_user.id, texts.CHECK_NOT_FOUND)
        return
    await _check_and_reply(bot, shop, call.from_user.id, payment_id)


async def _check_and_reply(bot: Bot, shop: Shop, user_id: int, payment_id: str):
    try:
        status = await shop.process_payment(payment_id)
    except Exception:
        log.exception("Ошибка проверки платежа %s", payment_id)
        await _send(bot, user_id, texts.CHECK_ERROR)
        return
    if status in {"succeeded", "in_progress"}:
        return  # благодарность и файл уже отправлены или отправляются прямо сейчас
    if status == "already_delivered":
        await _send(bot, user_id, texts.PAID_ALREADY_SENT, reply_markup=main_kb(shop, True))
    elif status == "delivery_failed":
        await _send(bot, user_id, texts.CHECK_DELIVERY_RETRY)
    elif status == "canceled":
        await _send(bot, user_id, texts.CHECK_CANCELED, reply_markup=main_kb(shop, False))
    else:
        await _send(bot, user_id, texts.CHECK_PENDING)


# ---------- информация ----------
@router.callback_query(F.data == "about")
async def about(call: CallbackQuery, shop: Shop, bot: Bot):
    await call.answer()
    paid = await shop.db.has_paid(call.from_user.id)
    await _send(bot, call.from_user.id, texts.ABOUT, reply_markup=main_kb(shop, paid))


@router.callback_query(F.data == "support")
async def support(call: CallbackQuery, shop: Shop, bot: Bot):
    await call.answer()
    await _send(bot, call.from_user.id, _support_text(shop, call.from_user.id))


@router.message(Command("help", "support"))
async def support_cmd(message: Message, shop: Shop):
    await message.answer(_support_text(shop, message.from_user.id), disable_web_page_preview=True)


def _support_text(shop: Shop, user_id: int) -> str:
    if shop.cfg.support_username:
        text = texts.SUPPORT.format(username=html.escape(shop.cfg.support_username), user_id=user_id)
    else:
        text = texts.SUPPORT_NO_CONTACT.format(user_id=user_id)
    if shop.cfg.seller_info:
        text += texts.SELLER.format(seller=html.escape(shop.cfg.seller_info))
    return text + _offer_note(shop)


# ---------- мои покупки ----------
@router.callback_query(F.data == "my")
async def my_cb(call: CallbackQuery, shop: Shop, bot: Bot):
    await call.answer()
    await _send_my(bot, shop, call.from_user.id)


@router.message(Command("my"))
async def my_cmd(message: Message, shop: Shop, bot: Bot):
    await _send_my(bot, shop, message.from_user.id)


async def _send_my(bot: Bot, shop: Shop, user_id: int):
    if await shop.db.has_paid(user_id):
        await _send_product(bot, shop, user_id)
    else:
        await _send(bot, user_id, texts.NO_PURCHASES, reply_markup=main_kb(shop, False))


# ---------- администратор ----------
def _is_admin(shop: Shop, user_id: int) -> bool:
    return user_id in shop.cfg.admin_ids


@router.message(Command("stats"))
async def stats(message: Message, shop: Shop):
    if not _is_admin(shop, message.from_user.id):
        return await fallback(message, shop)
    s = await shop.db.stats()
    conv = (s["buyers"] / s["users"] * 100) if s["users"] else 0
    lines = [
        "<b>📊 Статистика</b>",
        f"Пользователей: <b>{s['users']}</b> (сегодня +{s['today_users']})",
        f"Покупателей: <b>{s['buyers']}</b> · конверсия {conv:.1f}%",
        f"Продаж: <b>{s['sales']}</b> (сегодня {s['today_sales']})",
        f"Выручка: <b>{s['revenue']:,.0f} ₽</b>".replace(",", " "),
        f"Ожидают оплаты: {s['pending']} · выдано вручную: {s['manual']}",
    ]
    if s["test_sales"]:
        lines.append(f"Тестовых оплат (не входят в выручку): {s['test_sales']}")
    if s["undelivered"]:
        lines.append(f"⚠️ Оплачено, но не выдано: <b>{s['undelivered']}</b> — бот повторяет отправку")
    lines += ["", "<b>Источники трафика</b> (пользователи → покупатели):"]
    for src in s["sources"]:
        lines.append(f"• {html.escape(str(src['src']))}: {src['users']} → {src['buyers']}")
    await message.answer("\n".join(lines))


@router.message(Command("grant"))
async def grant(message: Message, command: CommandObject, shop: Shop):
    if not _is_admin(shop, message.from_user.id):
        return await fallback(message, shop)
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: <code>/grant 123456789</code> — выдать гайд пользователю по ID.")
        return
    user_id = int(arg)
    if await shop.db.get_user(user_id) is None:
        await message.answer(f"Пользователь {user_id} ещё не запускал бота — Telegram не даст отправить ему файл. "
                             "Попросите его нажать /start и повторите команду.")
        return
    status = await shop.grant(user_id)
    if status == "succeeded":
        await message.answer(f"Гайд выдан пользователю {user_id} ✅")
    else:
        await message.answer(f"Пользователю {user_id} не удалось отправить файл (возможно, он заблокировал бота). "
                             "Бот повторит отправку автоматически и отдаст гайд, когда пользователь откроет бота.")


@router.message(Command("check"))
async def admin_check(message: Message, command: CommandObject, shop: Shop):
    """Ручная проверка счёта по номеру (InvId) из личного кабинета Робокассы."""
    if not _is_admin(shop, message.from_user.id):
        return await fallback(message, shop)
    pid = (command.args or "").strip()
    if not shop.cfg.payments_enabled:
        await message.answer("Данные Робокассы ещё не указаны в .env — проверять нечего.")
        return
    if not pid.isdigit():
        await message.answer("Использование: <code>/check номер_счёта</code>")
        return
    status = await shop.process_payment(pid)
    await message.answer(f"Счёт <code>{pid}</code>: {texts.ADMIN_STATUS.get(status, status)}")


@router.message()
async def fallback(message: Message, shop: Shop):
    paid = await shop.db.has_paid(message.from_user.id)
    await message.answer(texts.FALLBACK, reply_markup=main_kb(shop, paid))


# ---------- ошибки ----------
@router.errors()
async def on_error(event: ErrorEvent, bot: Bot):
    """Любая непредвиденная ошибка: записываем в журнал и вежливо сообщаем пользователю,
    вместо того чтобы молча не ответить."""
    log.error("Ошибка при обработке обновления: %s", event.exception, exc_info=event.exception)
    upd = event.update
    user = (upd.message and upd.message.from_user) or (upd.callback_query and upd.callback_query.from_user)
    if upd.callback_query:
        with contextlib.suppress(Exception):
            await upd.callback_query.answer()
    if user:
        with contextlib.suppress(Exception):
            await bot.send_message(user.id, texts.GENERIC_ERROR)
    return True
