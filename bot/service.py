"""Бизнес-логика: заказ, оплата через ЮKassa и выдача гайда.

Путь покупки: «Купить» → бот создаёт заказ и платёж в ЮKassa → покупатель платит на странице
ЮKassa → ЮKassa присылает уведомление на /pay/notify → бот перепроверяет платёж через API ЮKassa,
фиксирует оплату и сразу отвечает, а гайд отправляет в фоне.

Гарантии:
- Гайд выдаётся только после того, как API ЮKassa подтвердил платёж: статус succeeded, paid=true,
  сумма и валюта заказа, метаданные заказа и покупателя, нужный магазин, не тестовый платёж
  в боевом режиме. Содержимому уведомления бот не доверяет.
- У покупателя одновременно существует только одна неоплаченная ссылка: прежде чем создать
  новый платёж, бот спрашивает у ЮKassa статус прежнего.
- Платёж создаётся с ключом идемпотентности = номер заказа: повтор после сбоя не создаёт дубль.
- Если покупатель всё же оплатил дважды (две вкладки, старая ссылка), фиксируется один платёж,
  а лишний автоматически возвращается.
- Все пути выдачи сходятся в _deliver, которая атомарно отмечает выдачу, — гайд приходит ровно
  один раз. Если отправить файл не удалось, фоновая задача повторяет выдачу сутки, а при
  следующем /start пользователь получает гайд сразу.
"""
import asyncio
import contextlib
import html
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile

from . import texts
from .config import Config
from .db import CANCELED, DELIVERED, DUPLICATE, PENDING, REFUNDED, REVIEW, SENDING, SUCCEEDED, Database
from .watermark import stamp_pdf
from .yookassa import CANCELED as YK_CANCELED
from .yookassa import PENDING as YK_PENDING
from .yookassa import SUCCEEDED as YK_SUCCEEDED
from .yookassa import WAITING as YK_WAITING
from .yookassa import Payment, YooKassa, YooKassaError

log = logging.getLogger(__name__)

# Повторы выдачи: паузы между попытками (сек). Последняя пауза повторяется до MAX_DELIVERY_ATTEMPTS.
RETRY_DELAYS = (15, 30, 60, 120, 300, 600, 1800, 3600)
MAX_DELIVERY_ATTEMPTS = 30  # ≈ сутки попыток
# Фоновая проверка неоплаченных заказов: до 3 суток после создания, старше часа — раз в 10 минут
LATE_PAYMENT_HOURS = 72
LATE_CHECK_EVERY_MIN = 10
POLL_CONCURRENCY = 4
# Ключ идемпотентности ЮKassa действует 24 часа: незавершённое создание платежа повторяем
# с тем же ключом не позже чем через 23 часа, иначе заказ закрывается и создаётся новый
IDEMPOTENCE_SAFE_HOURS = 23
MAX_REFUND_ATTEMPTS = 20
# Резервные копии базы
BACKUP_KEEP = 14


class AlreadyPaid(Exception):
    """У покупателя уже есть оплаченный заказ — новый платёж не создаётся."""


class PaymentInReview(Exception):
    """Деньги покупателя получены, но платёж на ручной проверке — новую оплату не принимаем."""


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


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


class Shop:
    def __init__(self, config: Config, db: Database, pay: YooKassa, bot: Bot):
        self.cfg = config
        self.db = db
        self.pay = pay
        self.bot = bot
        self.bot_username: str = ""
        self._file_id: str | None = None
        self._locks: dict[str, list] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_backup: str | None = None

    @contextlib.asynccontextmanager
    async def _lock(self, key: str):
        """Блокировка по ключу (заказ или пользователь). Освобождённые блокировки удаляются,
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

    # ---------- заказ и ссылка на оплату ----------
    @staticmethod
    def make_order_id(number: int, user_id: int) -> str:
        return f"{number}-{user_id}"

    async def get_or_create_payment(self, user_id: int) -> tuple[str, str]:
        """Возвращает (номер заказа, ссылка на оплату).

        Повторное нажатие «Купить» возвращает ту же ссылку, пока платёж в ЮKassa не оплачен и не
        отменён. AlreadyPaid — оплата уже есть (в том числе если её нашли прямо сейчас).
        PaymentInReview — платёж покупателя на проверке, второй раз брать деньги нельзя.
        YooKassaError — ЮKassa недоступна; повторное нажатие безопасно."""
        async with self._lock(f"user:{user_id}"):
            if await self.db.has_paid(user_id):
                raise AlreadyPaid
            if await self.db.has_status(user_id, REVIEW):
                raise PaymentInReview
            order = await self.db.open_order(user_id)
            if order is not None:
                reused = await self._reuse_order(order)
                if reused:
                    return order["payment_id"], reused
            order_id = self.make_order_id(await self.db.next_inv_id(), user_id)
            await self.db.add_payment(order_id, user_id, self.cfg.price_str, PENDING, None, is_test=self.cfg.test_mode)
            log.info("Создан заказ %s для %s", order_id, user_id)
            url = await self._create_remote(await self.db.get_payment(order_id))
            return order_id, url

    async def _create_remote(self, order) -> str:
        """Создаёт платёж в ЮKassa для заказа. Ключ идемпотентности = номер заказа, поэтому повтор
        после обрыва связи вернёт тот же платёж, а не создаст второй."""
        payment = await self.pay.create_payment(
            order_id=order["payment_id"], user_id=order["user_id"], amount=order["amount"],
            description=f"{self.cfg.payment_description} (заказ {order['payment_id']})",
            return_url=self.cfg.return_url, idempotence_key=order["payment_id"])
        if not payment.confirmation_url:
            raise YooKassaError("ЮKassa не вернула ссылку на оплату")
        await self.db.set_provider(order["payment_id"], payment.id, payment.confirmation_url)
        log.info("Заказ %s: платёж ЮKassa %s", order["payment_id"], payment.id)
        return payment.confirmation_url

    async def _reuse_order(self, order) -> str | None:
        """Решает судьбу прежнего неоплаченного заказа. Ссылка — если им можно продолжить пользоваться,
        None — заказ закрыт и нужен новый. AlreadyPaid — оказалось, что он оплачен."""
        pid = order["payment_id"]
        age = datetime.now(timezone.utc) - _parse_time(order["created_at"])
        if not order["provider_id"]:
            # платёж в ЮKassa не создался (сбой связи) — повторяем с тем же ключом идемпотентности
            if age < timedelta(hours=IDEMPOTENCE_SAFE_HOURS) and _same_amount(order["amount"], self.cfg.price_str) \
                    and bool(order["is_test"]) == self.cfg.test_mode:
                return await self._create_remote(order)
            await self.db.set_status(pid, CANCELED, note="платёж не создан")
            return None
        try:
            remote = await self.pay.get_payment(order["provider_id"])
        except YooKassaError as e:
            if e.not_found:  # платёж другого магазина (сменили тестовый/боевой) — закрываем
                await self.db.set_status(pid, CANCELED, note="платёж не найден в ЮKassa")
                return None
            if age < timedelta(minutes=self.cfg.payment_ttl_min) and order["confirmation_url"]:
                log.warning("ЮKassa недоступна (%s), отдаю сохранённую ссылку заказа %s", e, pid)
                return order["confirmation_url"]
            raise
        outcome = await self.apply_payment(order, remote)
        if outcome in {"paid", "already"}:
            raise AlreadyPaid
        if outcome == "review":
            raise PaymentInReview
        if remote.status == YK_PENDING and outcome == "pending":
            return remote.confirmation_url or order["confirmation_url"]
        return None  # отменён, на проверке или возвращён — создаём новый заказ

    # ---------- проверка платежа (единая точка для всех путей) ----------
    def _verify(self, order, remote: Payment) -> str | None:
        """Причина, по которой успешный платёж нельзя засчитать, или None."""
        if remote.id != order["provider_id"]:
            return f"ID платежа {remote.id} не совпадает с заказом"
        if remote.account_id and remote.account_id != self.cfg.shop_id:
            return f"платёж другого магазина ({remote.account_id})"
        if remote.currency != "RUB" or remote.amount is None or not _same_amount(remote.amount, order["amount"]):
            return f"сумма {remote.amount} {remote.currency} вместо {order['amount']} RUB"
        if remote.metadata.get("order_id") != order["payment_id"] or remote.metadata.get("user_id") != str(order["user_id"]):
            return "метаданные платежа не совпадают с заказом"
        if remote.test and not self.cfg.test_mode:
            return "тестовый платёж в боевом режиме"
        return None

    async def apply_payment(self, order, remote: Payment) -> str:
        """Применяет к заказу состояние платежа из API ЮKassa.

        Возвращает: paid — оплата зафиксирована сейчас (гайд отправляется); already — была
        зафиксирована раньше; duplicate — лишняя оплата, деньги возвращаются; review — платёж
        не прошёл проверку; refunded — деньги возвращены; canceled; pending."""
        pid = order["payment_id"]
        async with self._lock(f"apply:{order['user_id']}"):
            order = await self.db.get_payment(pid)  # свежее состояние под блокировкой
            await self.db.mark_checked(pid)
            # магазин настроен на двухстадийные платежи — подтверждаем, только если всё сходится;
            # иначе деньги остаются заблокированными и ЮKassa сама вернёт их через несколько дней
            if remote.status == YK_WAITING and order["status"] == PENDING:
                reason = self._verify(order, remote)
                if reason:
                    return await self._to_review(order, remote, reason + " (платёж не подтверждён)")
                remote = await self.pay.capture(remote)
            if remote.status in (YK_PENDING, YK_WAITING):
                return "pending"
            if remote.status == YK_CANCELED:
                await self.db.set_status(pid, CANCELED, note=remote.cancellation_reason or None)
                return "refunded" if order["status"] == REFUNDED else "canceled"
            if remote.status != YK_SUCCEEDED or not remote.paid:
                log.warning("Заказ %s: неизвестный статус платежа %s", pid, remote.status)
                return "pending"
            if remote.refunded >= (remote.amount or Decimal("0.01")):
                return await self._mark_refunded(order, remote)
            if order["status"] in {REVIEW, DUPLICATE, REFUNDED}:
                return order["status"] if order["status"] != REFUNDED else "refunded"
            reason = self._verify(order, remote)
            if reason:
                return await self._to_review(order, remote, reason)
            outcome = await self.db.settle_paid(pid)
        if outcome == "paid":
            log.info("Заказ %s оплачен (платёж ЮKassa %s)", pid, remote.id)
            self.deliver_in_background(pid)
            return "paid"
        if outcome == "duplicate":
            log.warning("Заказ %s: повторная оплата покупателем %s", pid, order["user_id"])
            await self._refund_duplicate(pid, remote, first=True)
            return "duplicate"
        return "already" if outcome == "already" else order["status"]

    async def _to_review(self, order, remote: Payment, reason: str) -> str:
        """Платёж не прошёл проверку: гайд не выдаётся, администратор получает тревогу (один раз)."""
        pid = order["payment_id"]
        if await self.db.set_status(pid, REVIEW, only_from=(PENDING, CANCELED), note=reason):
            log.error("Заказ %s: платёж %s не прошёл проверку: %s", pid, remote.id, reason)
            await self._notify_admins(texts.ADMIN_PAYMENT_REVIEW.format(
                payment_id=pid, provider_id=remote.id, reason=html.escape(reason)))
        return "review"

    async def _mark_refunded(self, order, remote: Payment) -> str:
        pid = order["payment_id"]
        changed = await self.db.set_status(pid, REFUNDED, only_from=(PENDING, CANCELED, SUCCEEDED, REVIEW))
        if order["status"] == DUPLICATE:
            await self.db.set_refund_state(pid, "succeeded")
        elif changed:
            log.warning("Заказ %s: деньги возвращены (платёж %s)", pid, remote.id)
            await self._notify_admins(texts.ADMIN_REFUNDED.format(payment_id=pid, user_id=order["user_id"],
                                                                  amount=remote.refunded))
        return "refunded"

    async def _refund_duplicate(self, pid: str, remote: Payment, first: bool = False) -> None:
        """Лишняя оплата: возвращаем деньги (ключ идемпотентности не даст вернуть дважды)."""
        order = await self.db.get_payment(pid)
        if not self.cfg.auto_refund_duplicates:
            await self.db.set_refund_state(pid, "manual")
            if first:
                await self._notify_admins(texts.ADMIN_DUPLICATE_MANUAL.format(
                    payment_id=pid, provider_id=remote.id, user_id=order["user_id"], amount=order["amount"]))
                with contextlib.suppress(Exception):
                    await self.bot.send_message(order["user_id"], texts.DUPLICATE_PAYMENT_MANUAL)
            return
        try:
            await self.pay.refund(remote, f"Повторная оплата заказа {pid}: гайд уже был выдан")
        except YooKassaError as e:
            await self.db.set_refund_state(pid, "error", attempt=True)
            log.error("Заказ %s: не удалось вернуть повторную оплату: %s", pid, e)
            if first:
                await self._notify_admins(texts.ADMIN_DUPLICATE_REFUND_FAILED.format(
                    payment_id=pid, provider_id=remote.id, user_id=order["user_id"], error=html.escape(str(e)[:200])))
            return
        await self.db.set_refund_state(pid, "requested", attempt=True)
        log.info("Заказ %s: повторная оплата возвращается", pid)
        await self._notify_admins(texts.ADMIN_DUPLICATE_REFUNDED.format(
            payment_id=pid, user_id=order["user_id"], amount=order["amount"]))
        with contextlib.suppress(Exception):
            await self.bot.send_message(order["user_id"], texts.DUPLICATE_PAYMENT_REFUNDED.format(amount=order["amount"]))

    async def sync_order(self, payment_id: str) -> str:
        """Запрашивает у ЮKassa состояние платежа заказа и применяет его. Ошибка связи — YooKassaError."""
        async with self._lock(payment_id):
            order = await self.db.get_payment(payment_id)
            if order is None:
                return "unknown"
            if order["status"] != PENDING or not order["provider_id"]:
                return order["status"]
            remote = await self.pay.get_payment(order["provider_id"])
            return await self.apply_payment(order, remote)

    # ---------- уведомление ЮKassa ----------
    async def handle_notification(self, event: str, provider_id: str) -> str:
        """Уведомление ЮKassa: событие и ID платежа. Само уведомление не проверяется на подлинность —
        бот запрашивает платёж через API и верит только ответу API.
        Возвращает ignored (платёж не наш) или результат применения."""
        order = await self.db.by_provider(provider_id)
        if order is None:
            log.info("Уведомление %s о чужом платеже %s — пропускаю", event, provider_id)
            return "ignored"
        async with self._lock(order["payment_id"]):
            remote = await self.pay.get_payment(provider_id)  # YooKassaError → 500, ЮKassa повторит
            return await self.apply_payment(await self.db.get_payment(order["payment_id"]), remote)

    def deliver_in_background(self, payment_id: str) -> None:
        task = asyncio.create_task(self._deliver_safe(payment_id), name=f"deliver-{payment_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver_safe(self, payment_id: str) -> None:
        try:
            await self._deliver(payment_id)
        except Exception:
            # оплата уже отмечена — фоновая проверка повторит выдачу
            log.exception("Ошибка фоновой выдачи по заказу %s", payment_id)

    async def wait_background(self, timeout: float = 15) -> None:
        """При остановке бота даём закончиться начатым выдачам."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    # ---------- проверка заказа (кнопка «Я оплатил», возврат со страницы оплаты, /check) ----------
    async def process_payment(self, payment_id: str) -> str:
        """Проверяет заказ (при необходимости — через API ЮKassa) и выдаёт гайд, если он оплачен.

        Возвращает: succeeded — гайд только что отправлен; already_delivered — гайд был выдан раньше;
        in_progress — гайд отправляется прямо сейчас; delivery_failed — оплачен, но файл не отправился
        (бот повторит); duplicate — лишняя оплата, деньги возвращаются; review — платёж на проверке;
        refunded; pending / canceled / unknown — оплаты нет. Ошибка связи с ЮKassa — YooKassaError."""
        row = await self.db.get_payment(payment_id)
        if row is None:
            return "unknown"
        if row["status"] == PENDING and row["provider_id"] and self.cfg.payments_enabled:
            await self.sync_order(payment_id)
            await self.wait_background(timeout=10)
            row = await self.db.get_payment(payment_id)
        if row["status"] != SUCCEEDED:
            return row["status"] if row["status"] in {PENDING, CANCELED, DUPLICATE, REVIEW, REFUNDED} else "pending"
        return await self._deliver(payment_id)

    async def deliver_pending_for_user(self, user_id: int) -> bool:
        """Пользователь пришёл в бота (например, разблокировал его) — сразу отдаём невыданный гайд."""
        sent = False
        for pid in await self.db.undelivered_for_user(user_id):
            await self.db.reset_attempts(pid)
            sent = await self._deliver(pid) == "succeeded" or sent
        return sent

    async def poll_pending(self) -> None:
        """Фоновая задача: повторная выдача, если файл не отправился, и резервные копии."""
        while True:
            await asyncio.sleep(self.cfg.poll_interval_sec)
            await self.poll_once()

    async def poll_once(self) -> None:
        """Один проход фоновой проверки: платежи, по которым не пришло уведомление; невыданные
        гайды; невозвращённые повторные оплаты; копия базы."""
        try:
            if self.cfg.payments_enabled:
                await self._poll_payments()
                await self._retry_refunds()
            await self.db.expire_unsent(IDEMPOTENCE_SAFE_HOURS)
            await self.db.expire_stale(LATE_PAYMENT_HOURS)
            for pid in await self.db.undelivered_paid(MAX_DELIVERY_ATTEMPTS):
                try:
                    await self._deliver(pid)
                except Exception:
                    log.exception("Ошибка повторной выдачи по заказу %s", pid)
            await self._daily_backup()
        except Exception:
            log.exception("Ошибка фоновой проверки")

    async def _poll_payments(self) -> None:
        pending = await self.db.pending_for_polling(self.cfg.payment_ttl_min, LATE_PAYMENT_HOURS, LATE_CHECK_EVERY_MIN)
        sem = asyncio.Semaphore(POLL_CONCURRENCY)

        async def check(pid: str) -> None:
            async with sem:
                try:
                    await self.sync_order(pid)
                except YooKassaError as e:
                    log.warning("Не удалось проверить заказ %s: %s", pid, e)
                except Exception:
                    log.exception("Ошибка проверки заказа %s", pid)

        await asyncio.gather(*(check(pid) for pid in pending))

    async def _retry_refunds(self) -> None:
        for pid in await self.db.refunds_to_retry(MAX_REFUND_ATTEMPTS):
            order = await self.db.get_payment(pid)
            try:
                remote = await self.pay.get_payment(order["provider_id"])
            except YooKassaError as e:
                log.warning("Возврат по заказу %s: ЮKassa недоступна: %s", pid, e)
                continue
            if remote.refunded >= (remote.amount or Decimal("0.01")):
                await self.db.set_refund_state(pid, "succeeded")
            elif remote.status == YK_SUCCEEDED:
                await self._refund_duplicate(pid, remote)

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
        """Отправляет гайд по оплаченному заказу ровно один раз.
        Возвращает succeeded / already_delivered / in_progress / delivery_failed."""
        if not await self.db.claim_delivery(payment_id):
            row = await self.db.get_payment(payment_id)
            if row is None or row["status"] != SUCCEEDED:
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
            log.warning("Заказ %s: гайд не отправлен пользователю %s (попытка %s): %s",
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
        log.info("Заказ %s: гайд выдан пользователю %s", payment_id, user_id)
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
        await self.db.add_payment(pid, user_id, "0.00", SUCCEEDED, None, manual=True)
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
