"""Обработчики сообщений и кнопок."""
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import texts
from .service import Shop

log = logging.getLogger(__name__)
router = Router()


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


# ---------- /start ----------
@router.message(CommandStart())
async def start(message: Message, command: CommandObject, shop: Shop):
    user = message.from_user
    payload = (command.args or "").strip()[:64] or None
    source = payload if payload and payload not in {"paid", "fail"} else None
    await shop.db.upsert_user(user.id, user.username, user.first_name, source)

    # Пользователь вернулся со страницы оплаты (Success URL / Fail URL Робокассы)
    if payload == "paid":
        if await shop.db.has_paid(user.id):
            await shop.send_product(user.id)
            return
        pending = await shop.db.latest_pending(user.id)
        if pending:
            await _check_and_reply(message, shop, pending["payment_id"])
            return
    if payload == "fail" and not await shop.db.has_paid(user.id):
        await message.answer(texts.PAY_FAILED, reply_markup=main_kb(shop, False))
        return

    paid = await shop.db.has_paid(user.id)
    if paid:
        await message.answer(texts.ALREADY_BOUGHT, reply_markup=main_kb(shop, True))
        await shop.send_product(user.id)
        return
    name = html.escape(user.first_name or "друг")
    await message.answer(texts.WELCOME.format(name=name, price=_price(shop)), reply_markup=main_kb(shop, False))


# ---------- покупка ----------
@router.callback_query(F.data == "buy")
async def buy(call: CallbackQuery, shop: Shop):
    await call.answer()
    user_id = call.from_user.id
    if await shop.db.has_paid(user_id):
        await call.message.answer(texts.ALREADY_BOUGHT)
        await shop.send_product(user_id)
        return
    if not shop.cfg.payments_enabled:
        # Режим витрины: тот же экран оплаты, но кнопка пока не ведёт в кассу
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_PAY.format(price=_price(shop)), callback_data="soon")]])
        await call.message.answer(_pay_text(shop), reply_markup=kb, disable_web_page_preview=True)
        return
    try:
        payment_id, url = await shop.get_or_create_payment(user_id)
    except Exception:
        log.exception("Не удалось создать платёж для %s", user_id)
        await call.message.answer(texts.CREATE_ERROR)
        return
    await call.message.answer(_pay_text(shop), reply_markup=pay_kb(shop, url, payment_id), disable_web_page_preview=True)


def _pay_text(shop: Shop) -> str:
    text = texts.PAY.format(price=_price(shop))
    if shop.cfg.offer_url:
        text += texts.OFFER_NOTE.format(url=shop.cfg.offer_url)
    return text


@router.callback_query(F.data == "soon")
async def soon(call: CallbackQuery):
    await call.answer(texts.PAYMENTS_SOON, show_alert=True)


@router.callback_query(F.data.startswith("check:"))
async def check(call: CallbackQuery, shop: Shop):
    await call.answer("Проверяю оплату…")
    payment_id = call.data.split(":", 1)[1]
    row = await shop.db.get_payment(payment_id)
    if row is None or row["user_id"] != call.from_user.id:
        await call.message.answer(texts.CHECK_NOT_FOUND)
        return
    await _check_and_reply(call.message, shop, payment_id)


async def _check_and_reply(message: Message, shop: Shop, payment_id: str):
    try:
        status = await shop.process_payment(payment_id)
    except Exception:
        log.exception("Ошибка проверки платежа %s", payment_id)
        await message.answer(texts.CHECK_ERROR)
        return
    if status == "succeeded":
        return  # благодарность и файл уже отправлены (или были отправлены ранее)
    if status == "canceled":
        await message.answer(texts.CHECK_CANCELED, reply_markup=main_kb(shop, False))
    else:
        await message.answer(texts.CHECK_PENDING)


# ---------- информация ----------
@router.callback_query(F.data == "about")
async def about(call: CallbackQuery, shop: Shop):
    await call.answer()
    await call.message.answer(texts.ABOUT, reply_markup=main_kb(shop, False))


@router.callback_query(F.data == "support")
async def support(call: CallbackQuery, shop: Shop):
    await call.answer()
    await call.message.answer(_support_text(shop, call.from_user.id))


@router.message(Command("help", "support"))
async def support_cmd(message: Message, shop: Shop):
    await message.answer(_support_text(shop, message.from_user.id))


def _support_text(shop: Shop, user_id: int) -> str:
    if shop.cfg.support_username:
        text = texts.SUPPORT.format(username=shop.cfg.support_username, user_id=user_id)
    else:
        text = texts.SUPPORT_NO_CONTACT.format(user_id=user_id)
    if shop.cfg.seller_info:
        text += texts.SELLER.format(seller=html.escape(shop.cfg.seller_info))
    if shop.cfg.offer_url:
        text += texts.OFFER_NOTE.format(url=shop.cfg.offer_url)
    return text


# ---------- мои покупки ----------
@router.callback_query(F.data == "my")
async def my_cb(call: CallbackQuery, shop: Shop):
    await call.answer()
    await _send_my(call.message, shop, call.from_user.id)


@router.message(Command("my"))
async def my_cmd(message: Message, shop: Shop):
    await _send_my(message, shop, message.from_user.id)


async def _send_my(message: Message, shop: Shop, user_id: int):
    if await shop.db.has_paid(user_id):
        await shop.send_product(user_id)
    else:
        await message.answer(texts.NO_PURCHASES, reply_markup=main_kb(shop, False))


# ---------- администратор ----------
def _is_admin(shop: Shop, user_id: int) -> bool:
    return user_id in shop.cfg.admin_ids


@router.message(Command("stats"))
async def stats(message: Message, shop: Shop):
    if not _is_admin(shop, message.from_user.id):
        return
    s = await shop.db.stats()
    conv = (s["buyers"] / s["users"] * 100) if s["users"] else 0
    lines = [
        "<b>📊 Статистика</b>",
        f"Пользователей: <b>{s['users']}</b> (сегодня +{s['today_users']})",
        f"Покупателей: <b>{s['buyers']}</b> · конверсия {conv:.1f}%",
        f"Продаж: <b>{s['sales']}</b> (сегодня {s['today_sales']})",
        f"Выручка: <b>{s['revenue']:,.0f} ₽</b>".replace(",", " "),
        f"Ожидают оплаты: {s['pending']} · выдано вручную: {s['manual']}",
        "",
        "<b>Источники трафика</b> (пользователи → покупатели):",
    ]
    for src in s["sources"]:
        lines.append(f"• {html.escape(str(src['src']))}: {src['users']} → {src['buyers']}")
    await message.answer("\n".join(lines))


@router.message(Command("grant"))
async def grant(message: Message, command: CommandObject, shop: Shop):
    if not _is_admin(shop, message.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: <code>/grant 123456789</code> — выдать гайд пользователю по ID.")
        return
    user_id = int(command.args.strip())
    try:
        await shop.grant(user_id)
        await message.answer(f"Гайд выдан пользователю {user_id} ✅")
    except Exception as e:
        await message.answer(f"Не получилось: {html.escape(str(e))}. Пользователь должен сначала запустить бота.")


@router.message(Command("check"))
async def admin_check(message: Message, command: CommandObject, shop: Shop):
    """Ручная проверка счёта по номеру (InvId) из личного кабинета Робокассы."""
    if not _is_admin(shop, message.from_user.id):
        return
    pid = (command.args or "").strip()
    if not shop.cfg.payments_enabled:
        await message.answer("Данные Робокассы ещё не указаны в .env — проверять нечего.")
        return
    if not pid:
        await message.answer("Использование: <code>/check номер_счёта</code>")
        return
    status = await shop.process_payment(pid)
    await message.answer(f"Статус счёта <code>{html.escape(pid)}</code>: <b>{status}</b>")


@router.message()
async def fallback(message: Message, shop: Shop):
    paid = await shop.db.has_paid(message.from_user.id)
    await message.answer("Выберите действие 👇", reply_markup=main_kb(shop, paid))
