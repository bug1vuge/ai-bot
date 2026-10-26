"""Бизнес-логика: создание платежа, подтверждение оплаты и выдача гайда.

Оплату подтверждают три пути, и все они сходятся в одной функции _deliver, которая
атомарно отмечает выдачу — поэтому гайд приходит ровно один раз:
  1. уведомление Робокассы на Result URL (основной путь, мгновенно);
  2. кнопка «Я оплатил — проверить» и возврат со страницы оплаты;
  3. фоновая проверка неоплаченных счетов через API статуса Робокассы.
"""
import asyncio
import contextlib
import html
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile

from . import texts
from .config import Config
from .db import Database
from .robokassa import STATE_CANCELED, STATE_PAID, Robokassa
from .watermark import stamp_pdf

log = logging.getLogger(__name__)

# Сколько раз пытаться выдать оплаченный гайд, если Telegram не принимает файл
# (например, пользователь заблокировал бота). Попытки идут при каждой фоновой проверке.
MAX_DELIVERY_ATTEMPTS = 10


def _same_amount(a: str | None, b: str) -> bool:
    try:
        return a is not None and abs(float(a) - float(b)) < 0.005
    except ValueError:
        return False


class Shop:
    def __init__(self, config: Config, db: Database, kassa: Robokassa, bot: Bot):
        self.cfg = config
        self.db = db
        self.kassa = kassa
        self.bot = bot
        self.bot_username: str = ""
        self._file_id: str | None = None
        self._locks: dict[str, list] = {}

    @contextlib.asynccontextmanager
    async def _lock(self, key: str):
        """Блокировка по ключу (счёт или пользователь). Освобождённые блокировки удаляются,
        чтобы словарь не рос бесконечно."""
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)

    # ---------- создание платежа ----------
    def _receipt(self) -> dict | None:
        if not self.cfg.rk_send_receipt:
            return None
        return {"items": [{"name": self.cfg.receipt_item_name[:128], "quantity": 1, "sum": self.cfg.price_rub,
                           "payment_method": "full_payment", "payment_object": "service", "tax": "none"}]}

    async def get_or_create_payment(self, user_id: int) -> tuple[str, str]:
        """Возвращает (номер счёта, ссылка на оплату). Повторно использует свежую неоплаченную ссылку."""
        # блокировка по пользователю: двойное нажатие «Купить» не создаёт два счёта
        async with self._lock(f"user:{user_id}"):
            existing = await self.db.fresh_pending(user_id, self.cfg.payment_ttl_min,
                                                   self.cfg.price_str, self.cfg.rk_test)
            if existing and existing["confirmation_url"]:
                return existing["payment_id"], existing["confirmation_url"]
            inv_id = await self.db.next_inv_id()
            url = self.kassa.payment_url(inv_id, self.cfg.price_str, self.cfg.payment_description,
                                         shp={"Shp_user": user_id}, receipt=self._receipt())
            await self.db.add_payment(str(inv_id), user_id, self.cfg.price_str, "pending", url,
                                      is_test=self.cfg.rk_test)
        log.info("Создан счёт %s для %s", inv_id, user_id)
        return str(inv_id), url

    # ---------- путь 1: уведомление Result URL ----------
    async def handle_result(self, data: dict) -> bool:
        """Обработка уведомления Робокассы. Подпись уже проверена. True — уведомление принято."""
        inv_id = str(data.get("InvId", "")).strip()
        async with self._lock(inv_id):
            row = await self.db.get_payment(inv_id)
            if row is None:
                user = str(data.get("Shp_user", ""))
                if not (inv_id.isdigit() and user.isdigit()):
                    log.error("Уведомление о неизвестном счёте %s без покупателя", inv_id)
                    return False
                # счёта нет в базе (например, база потерялась) — восстанавливаем из данных уведомления
                await self.db.add_payment(inv_id, int(user), self.cfg.price_str, "pending", None,
                                          is_test=self.cfg.rk_test)
                await self.db.ensure_inv_id_above(int(inv_id))
                row = await self.db.get_payment(inv_id)
                log.warning("Счёт %s восстановлен из уведомления для %s", inv_id, user)
            if str(data.get("Shp_user", row["user_id"])) != str(row["user_id"]):
                log.error("Счёт %s: покупатель в уведомлении не совпадает с базой", inv_id)
                return False
            if not _same_amount(data.get("OutSum"), row["amount"]):
                log.error("Счёт %s: сумма %s не совпадает с ожидаемой %s", inv_id, data.get("OutSum"), row["amount"])
                return False
            if row["status"] != "succeeded":
                await self.db.set_status(inv_id, "succeeded")
        await self._deliver(inv_id)
        return True

    # ---------- пути 2 и 3: проверка статуса ----------
    async def process_payment(self, payment_id: str) -> str:
        """Проверяет счёт и выдаёт гайд, если он оплачен.

        Возвращает: succeeded — гайд только что отправлен; already_delivered — счёт оплачен и гайд
        был выдан раньше; delivery_failed — оплачен, но отправить файл не удалось;
        pending / canceled / unknown — оплаты нет."""
        async with self._lock(payment_id):
            row = await self.db.get_payment(payment_id)
            if row is None:
                return "unknown"
            if row["status"] != "succeeded" and self.cfg.payments_enabled:
                try:
                    st = await self.kassa.op_state(payment_id)
                except Exception:
                    log.warning("Не удалось запросить статус счёта %s", payment_id, exc_info=True)
                    st = {"result": None, "state": None, "out_sum": None}
                if st["result"] == 0 and st["state"] == STATE_PAID:
                    if st["out_sum"] is not None and not _same_amount(st["out_sum"], row["amount"]):
                        log.error("Счёт %s оплачен на другую сумму: %s", payment_id, st["out_sum"])
                        return "unknown"
                    await self.db.set_status(payment_id, "succeeded")
                elif st["result"] == 0 and st["state"] == STATE_CANCELED:
                    await self.db.set_status(payment_id, "canceled")
                    return "canceled"
                else:
                    return "canceled" if row["status"] == "canceled" else "pending"
            elif row["status"] != "succeeded":
                return "pending"
        return await self._deliver(payment_id)

    async def poll_pending(self) -> None:
        """Фоновая проверка неоплаченных счетов — на случай, если уведомление не дошло."""
        while True:
            await asyncio.sleep(self.cfg.poll_interval_sec)
            await self.poll_once()

    async def poll_once(self) -> None:
        """Один проход: проверить неоплаченные счета и повторить выдачу оплаченных, но не выданных."""
        try:
            pending = await self.db.pending_for_polling(self.cfg.payment_ttl_min) if self.cfg.payments_enabled else []
            for pid in pending:
                try:
                    await self.process_payment(pid)
                except Exception:
                    log.exception("Ошибка проверки счёта %s", pid)
            for pid in await self.db.undelivered_paid(MAX_DELIVERY_ATTEMPTS):
                try:
                    await self._deliver(pid)
                except Exception:
                    log.exception("Ошибка повторной выдачи по счёту %s", pid)
        except Exception:
            log.exception("Ошибка фоновой проверки")

    # ---------- выдача ----------
    async def _deliver(self, payment_id: str) -> str:
        """Отправляет гайд по оплаченному счёту ровно один раз.
        Возвращает succeeded / already_delivered / delivery_failed."""
        if not await self.db.claim_delivery(payment_id):
            row = await self.db.get_payment(payment_id)
            return "already_delivered" if row and row["delivered"] else "delivery_failed"
        row = await self.db.get_payment(payment_id)
        user_id = row["user_id"]
        first_try = row["delivery_attempts"] == 0
        try:
            if not row["manual"] and first_try:
                await self.bot.send_message(user_id, texts.THANKS)
            await self.send_product(user_id)
        except Exception as e:
            log.exception("Ошибка выдачи для %s", user_id)
            attempts = await self.db.release_delivery(payment_id)  # повторим при следующей проверке
            # администратору — о первой неудаче и когда попытки закончились, без спама на каждом круге
            if attempts == 1 or attempts >= MAX_DELIVERY_ATTEMPTS:
                await self._notify_admins(texts.ADMIN_DELIVERY_FAIL.format(
                    user_id=user_id, payment_id=payment_id, error=html.escape(str(e)[:300]),
                    attempts=attempts, max_attempts=MAX_DELIVERY_ATTEMPTS))
            if attempts == 1:
                with contextlib.suppress(Exception):
                    await self.bot.send_message(user_id, texts.DELIVERY_ERROR)
            return "delivery_failed"
        if not row["manual"]:
            await self._notify_sale(row)
        return "succeeded"

    def _file_key(self) -> str:
        """Меняется вместе с файлом гайда и настройками — тогда сохранённый file_id не используется."""
        st = self.cfg.product_file.stat()
        return f"{st.st_size}:{int(st.st_mtime)}:{self.cfg.watermark}:{self.cfg.product_filename}"

    async def send_product(self, user_id: int) -> None:
        caption = self.cfg.product_title
        if self.cfg.watermark:
            # Экземпляр с водяным знаком собирается один раз; повторно отправляем его по file_id
            # из Telegram — это мгновенно и не нагружает сервер, сколько бы раз ни нажали «Мои покупки».
            key = self._file_key()
            cached = await self.db.get_cached_file(user_id, key)
            if cached:
                try:
                    await self.bot.send_document(user_id, cached, caption=caption)
                    return
                except Exception:
                    log.warning("Сохранённый файл для %s не отправился, собираю заново", user_id, exc_info=True)
            user = await self.db.get_user(user_id)
            text = texts.WATERMARK.format(who=self._who(user, user_id), user_id=user_id,
                                          date=datetime.now(timezone.utc).strftime("%d.%m.%Y"))
            data = await asyncio.to_thread(stamp_pdf, self.cfg.product_file, text)
            msg = await self.bot.send_document(
                user_id, BufferedInputFile(data, filename=self.cfg.product_filename), caption=caption)
            file_id = getattr(getattr(msg, "document", None), "file_id", None)
            if file_id and user is not None:
                await self.db.set_cached_file(user_id, key, file_id)
            return
        if self._file_id:
            await self.bot.send_document(user_id, self._file_id, caption=caption)
            return
        msg = await self.bot.send_document(
            user_id, FSInputFile(self.cfg.product_file, filename=self.cfg.product_filename), caption=caption)
        self._file_id = msg.document.file_id

    async def grant(self, user_id: int) -> None:
        """Ручная выдача администратором (подарок, спорная ситуация)."""
        pid = f"manual-{user_id}-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        await self.db.add_payment(pid, user_id, "0.00", "succeeded", None, manual=True)
        await self.db.set_status(pid, "succeeded")
        if await self._deliver(pid) != "succeeded":
            raise RuntimeError("Telegram не принял файл — бот повторит отправку автоматически")

    # ---------- служебное ----------
    @staticmethod
    def _who(user, user_id: int) -> str:
        if user is None:
            return f"ID {user_id}"
        if user["username"]:
            return f"@{user['username']}"
        return user["first_name"] or f"ID {user_id}"

    async def _notify_sale(self, row) -> None:
        user = await self.db.get_user(row["user_id"])
        await self._notify_admins(texts.ADMIN_SALE.format(
            amount=row["amount"], who=html.escape(self._who(user, row["user_id"])), user_id=row["user_id"],
            source=html.escape((user["source"] if user else None) or "—"), payment_id=row["payment_id"],
            mode=" (тестовый платёж)" if row["is_test"] else ""))

    async def _notify_admins(self, text: str) -> None:
        for admin in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin, text)
            except Exception:
                log.warning("Не удалось уведомить администратора %s", admin)
