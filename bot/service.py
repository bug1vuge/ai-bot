"""Бизнес-логика: создание счёта, подтверждение оплаты и выдача гайда.

Оплату подтверждают три независимых пути, и все они сходятся в одной функции _deliver,
которая атомарно отмечает выдачу — поэтому гайд приходит ровно один раз:
  1. уведомление Робокассы на Result URL (основной путь, мгновенно);
  2. кнопка «Я оплатил — проверить» и возврат со страницы оплаты;
  3. фоновая проверка неоплаченных счетов через API статуса Робокассы.
Если отправить файл не удалось (Telegram недоступен, пользователь заблокировал бота),
фоновая проверка повторяет выдачу с нарастающей паузой в течение суток, а при следующем
/start пользователь получает гайд сразу.
"""
import asyncio
import contextlib
import html
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile

from . import texts
from .config import Config
from .db import DELIVERED, SENDING, Database
from .robokassa import STATE_CANCELED, STATE_PAID, Robokassa
from .watermark import stamp_pdf

log = logging.getLogger(__name__)

# Повторы выдачи: паузы между попытками (сек). Последняя пауза повторяется до MAX_DELIVERY_ATTEMPTS.
RETRY_DELAYS = (15, 30, 60, 120, 300, 600, 1800, 3600)
MAX_DELIVERY_ATTEMPTS = 30  # ≈ сутки попыток
# Фоновая проверка старых неоплаченных счетов: до суток после создания, не чаще раза в 10 минут
LATE_PAYMENT_HOURS = 24
LATE_CHECK_EVERY_MIN = 10
# Сколько запросов статуса к Робокассе выполнять одновременно
POLL_CONCURRENCY = 4
# Резервные копии базы
BACKUP_KEEP = 14


def retry_delay(attempts: int) -> int:
    return RETRY_DELAYS[min(max(attempts, 1), len(RETRY_DELAYS)) - 1]


def _amount(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _same_amount(a, b) -> bool:
    x, y = _amount(a), _amount(b)
    return x is not None and x == y


class Shop:
    def __init__(self, config: Config, db: Database, kassa: Robokassa, bot: Bot):
        self.cfg = config
        self.db = db
        self.kassa = kassa
        self.bot = bot
        self.bot_username: str = ""
        self._file_id: str | None = None
        self._locks: dict[str, list] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_backup: str | None = None

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

    # ---------- создание счёта ----------
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
        """Уведомление Робокассы (подпись уже проверена): отмечаем оплату и сразу отвечаем.
        Гайд отправляется в фоне, чтобы медленный Telegram не задерживал ответ Робокассе.
        True — уведомление принято."""
        inv_id = str(data.get("InvId", "")).strip()
        user = str(data.get("Shp_user", "")).strip()
        async with self._lock(inv_id):
            row = await self.db.get_payment(inv_id)
            if row is None:
                if not (inv_id.isdigit() and user.isdigit()):
                    log.error("Уведомление о неизвестном счёте %s без покупателя", inv_id)
                    await self._notify_admins(texts.ADMIN_RESULT_REJECTED.format(
                        payment_id=html.escape(inv_id), reason="счёта нет в базе и не указан покупатель"))
                    return False
                # Счёта нет в базе (например, база потерялась при переезде) — восстанавливаем из
                # уведомления. Сумма подписана Робокассой, поэтому берём её из уведомления.
                amount = _amount(data.get("OutSum"))
                await self.db.add_payment(inv_id, int(user), f"{amount or self.cfg.price_str}", "pending", None,
                                          is_test=self.cfg.rk_test)
                await self.db.ensure_inv_id_above(int(inv_id))
                row = await self.db.get_payment(inv_id)
                log.warning("Счёт %s восстановлен из уведомления для %s", inv_id, user)
            if user and user != str(row["user_id"]):
                log.error("Счёт %s: покупатель в уведомлении (%s) не совпадает с базой", inv_id, user)
                await self._notify_admins(texts.ADMIN_RESULT_REJECTED.format(
                    payment_id=inv_id, reason=f"покупатель {html.escape(user)} не совпадает с {row['user_id']}"))
                return False
            if not _same_amount(data.get("OutSum"), row["amount"]):
                log.error("Счёт %s: сумма %s не совпадает с ожидаемой %s", inv_id, data.get("OutSum"), row["amount"])
                await self._notify_admins(texts.ADMIN_RESULT_REJECTED.format(
                    payment_id=inv_id, reason=f"сумма {html.escape(str(data.get('OutSum')))} вместо {row['amount']}"))
                return False
            if row["status"] != "succeeded":
                await self.db.mark_paid(inv_id)
                log.info("Счёт %s оплачен (уведомление Робокассы)", inv_id)
        self.deliver_in_background(inv_id)
        return True

    def deliver_in_background(self, payment_id: str) -> None:
        task = asyncio.create_task(self._deliver_safe(payment_id), name=f"deliver-{payment_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver_safe(self, payment_id: str) -> None:
        try:
            await self._deliver(payment_id)
        except Exception:
            # оплата уже отмечена — фоновая проверка повторит выдачу
            log.exception("Ошибка фоновой выдачи по счёту %s", payment_id)

    async def wait_background(self, timeout: float = 15) -> None:
        """При остановке бота даём закончиться начатым выдачам."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # ---------- пути 2 и 3: проверка статуса ----------
    async def process_payment(self, payment_id: str) -> str:
        """Проверяет счёт и выдаёт гайд, если он оплачен.

        Возвращает: succeeded — гайд только что отправлен; already_delivered — гайд был выдан раньше;
        in_progress — гайд отправляется прямо сейчас; delivery_failed — оплачен, но файл не отправился
        (бот повторит); pending / canceled / unknown — оплаты нет."""
        async with self._lock(payment_id):
            row = await self.db.get_payment(payment_id)
            if row is None:
                return "unknown"
            if row["status"] != "succeeded":
                if not self.cfg.payments_enabled or row["manual"]:
                    return "pending"
                try:
                    st = await self.kassa.op_state(payment_id)
                except Exception as e:
                    log.warning("Не удалось запросить статус счёта %s: %s", payment_id, e)
                    st = {"result": None, "state": None, "out_sum": None}
                await self.db.mark_checked(payment_id)
                if st["result"] == 0 and st["state"] == STATE_PAID:
                    if st["out_sum"] is not None and not _same_amount(st["out_sum"], row["amount"]):
                        log.error("Счёт %s оплачен на другую сумму: %s", payment_id, st["out_sum"])
                        return "unknown"
                    await self.db.mark_paid(payment_id)
                    log.info("Счёт %s оплачен (проверка статуса)", payment_id)
                elif st["result"] == 0 and st["state"] == STATE_CANCELED:
                    await self.db.mark_canceled(payment_id)
                    return "canceled"
                else:
                    return "canceled" if row["status"] == "canceled" else "pending"
        return await self._deliver(payment_id)

    async def deliver_pending_for_user(self, user_id: int) -> bool:
        """Пользователь пришёл в бота (например, разблокировал его) — сразу отдаём невыданный гайд."""
        sent = False
        for pid in await self.db.undelivered_for_user(user_id):
            await self.db.reset_attempts(pid)
            sent = await self._deliver(pid) == "succeeded" or sent
        return sent

    async def poll_pending(self) -> None:
        """Фоновая проверка — на случай, если уведомление не дошло или файл не отправился."""
        while True:
            await asyncio.sleep(self.cfg.poll_interval_sec)
            await self.poll_once()

    async def poll_once(self) -> None:
        """Один проход: проверить неоплаченные счета, повторить невыданное, сделать копию базы."""
        try:
            if self.cfg.payments_enabled:
                pending = await self.db.pending_for_polling(self.cfg.payment_ttl_min, LATE_PAYMENT_HOURS,
                                                            LATE_CHECK_EVERY_MIN)
                sem = asyncio.Semaphore(POLL_CONCURRENCY)

                async def check(pid: str) -> None:
                    async with sem:
                        try:
                            await self.process_payment(pid)
                        except Exception:
                            log.exception("Ошибка проверки счёта %s", pid)

                await asyncio.gather(*(check(pid) for pid in pending))
            for pid in await self.db.undelivered_paid(MAX_DELIVERY_ATTEMPTS):
                try:
                    await self._deliver(pid)
                except Exception:
                    log.exception("Ошибка повторной выдачи по счёту %s", pid)
            await self._daily_backup()
        except Exception:
            log.exception("Ошибка фоновой проверки")

    async def _daily_backup(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_backup == today:
            return
        folder = self.cfg.db_path.parent / "backups"
        try:
            await self.db.backup(folder / f"bot-{today}.db")
            for old in sorted(folder.glob("bot-*.db"))[:-BACKUP_KEEP]:
                old.unlink(missing_ok=True)
            self._last_backup = today
            log.info("Резервная копия базы: %s", folder / f"bot-{today}.db")
        except Exception:
            log.exception("Не удалось сделать резервную копию базы")
            self._last_backup = today  # не пытаться каждые 30 секунд

    # ---------- выдача ----------
    async def _deliver(self, payment_id: str) -> str:
        """Отправляет гайд по оплаченному счёту ровно один раз.
        Возвращает succeeded / already_delivered / in_progress / delivery_failed."""
        if not await self.db.claim_delivery(payment_id):
            row = await self.db.get_payment(payment_id)
            if row is None or row["status"] != "succeeded":
                return "pending"
            return {DELIVERED: "already_delivered", SENDING: "in_progress"}.get(row["delivered"], "delivery_failed")
        row = await self.db.get_payment(payment_id)
        user_id = row["user_id"]
        try:
            if not row["manual"] and row["delivery_attempts"] == 0:
                repeat = await self.db.count_delivered(user_id, exclude=payment_id)
                await self.bot.send_message(user_id, texts.REPEAT_PAYMENT if repeat else texts.THANKS)
                if repeat:
                    await self._notify_admins(texts.ADMIN_REPEAT_PAYMENT.format(
                        user_id=user_id, payment_id=payment_id, amount=row["amount"]))
            await self.send_product(user_id)
        except asyncio.CancelledError:
            await self.db.release_delivery(payment_id, 0)  # остановка бота: повторим после запуска
            raise
        except Exception as e:
            attempts = await self.db.release_delivery(payment_id, retry_delay(row["delivery_attempts"] + 1))
            log.warning("Счёт %s: гайд не отправлен пользователю %s (попытка %s): %s",
                        payment_id, user_id, attempts, e)
            # администратору — о первой неудаче и когда попытки закончились, без спама на каждом круге
            if attempts == 1 or attempts >= MAX_DELIVERY_ATTEMPTS:
                await self._notify_admins(texts.ADMIN_DELIVERY_FAIL.format(
                    user_id=user_id, payment_id=payment_id, error=html.escape(str(e)[:300]),
                    attempts=attempts, max_attempts=MAX_DELIVERY_ATTEMPTS))
            if attempts == 1 and not row["manual"]:
                with contextlib.suppress(Exception):
                    await self.bot.send_message(user_id, texts.DELIVERY_ERROR)
            return "delivery_failed"
        await self.db.finish_delivery(payment_id)
        log.info("Счёт %s: гайд выдан пользователю %s", payment_id, user_id)
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
            # из Telegram — это мгновенно и не нагружает сервер.
            key = self._file_key()
            cached = await self.db.get_cached_file(user_id, key)
            if cached:
                try:
                    await self.bot.send_document(user_id, cached, caption=caption)
                    return
                except Exception as e:
                    if not _is_bad_file_id(e):
                        raise  # сеть или пользователь заблокировал бота — пересборка не поможет
                    log.warning("Сохранённый файл для %s устарел, собираю заново: %s", user_id, e)
                    await self.db.clear_cached_file(user_id)
            user = await self.db.get_user(user_id)
            text = texts.WATERMARK.format(who=self._who(user, user_id), user_id=user_id,
                                          date=datetime.now(timezone.utc).strftime("%d.%m.%Y"))
            try:
                data = await asyncio.to_thread(stamp_pdf, self.cfg.product_file, text)
            except Exception as e:
                # водяной знак — защита, а не условие выдачи: покупатель должен получить гайд в любом случае
                log.exception("Не удалось поставить водяной знак, отправляю гайд без него")
                await self._notify_admins(texts.ADMIN_WATERMARK_FAIL.format(error=html.escape(str(e)[:300])))
                data = await asyncio.to_thread(self.cfg.product_file.read_bytes)
            msg = await self.bot.send_document(
                user_id, BufferedInputFile(data, filename=self.cfg.product_filename), caption=caption)
            file_id = getattr(getattr(msg, "document", None), "file_id", None)
            if file_id and user is not None:
                await self.db.set_cached_file(user_id, key, file_id)
            return
        if self._file_id:
            try:
                await self.bot.send_document(user_id, self._file_id, caption=caption)
                return
            except Exception as e:
                if not _is_bad_file_id(e):
                    raise
                self._file_id = None
        msg = await self.bot.send_document(
            user_id, FSInputFile(self.cfg.product_file, filename=self.cfg.product_filename), caption=caption)
        self._file_id = getattr(getattr(msg, "document", None), "file_id", None)

    async def grant(self, user_id: int) -> str:
        """Ручная выдача администратором (подарок, спорная ситуация). Возвращает статус выдачи."""
        pid = f"manual-{user_id}-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        await self.db.add_payment(pid, user_id, "0.00", "succeeded", None, manual=True)
        return await self._deliver(pid)

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
            except Exception as e:
                log.warning("Не удалось уведомить администратора %s: %s", admin, e)


def _is_bad_file_id(error: Exception) -> bool:
    """Telegram не узнал сохранённый file_id (а не сбой сети / блокировка бота)."""
    text = str(error).lower()
    return "file" in text and ("wrong" in text or "invalid" in text or "identifier" in text)
