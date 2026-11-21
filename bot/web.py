"""Веб-адреса бота:
  POST /pay/notify   — HTTP-уведомления ЮKassa (кабинет ЮKassa → Интеграция → HTTP-уведомления)
  GET  /pay/success  — сюда ЮKassa возвращает покупателя после оплаты → обратно в бота
  GET  /pay/return   — запасной адрес возврата → обратно в бота
  GET  /offer        — публичная оферта (текст в data/offer.md)
  GET  /health       — проверка, что бот жив
"""
import ipaddress
import json
import logging
import time

from aiohttp import web

from . import texts
from .offer import render_offer
from .service import Shop
from .yookassa import OBJECT_ID_RE, YooKassaError, is_yookassa_ip

log = logging.getLogger(__name__)

# Администратору о подозрительных уведомлениях — не чаще раза в столько секунд
ALERT_EVERY_SEC = 600
# События ЮKassa, которые обрабатывает бот; по остальным просто отвечаем 200
PAYMENT_EVENTS = {"payment.succeeded", "payment.waiting_for_capture", "payment.canceled"}
REFUND_EVENTS = {"refund.succeeded"}


def client_ip(request: web.Request) -> str:
    """Адрес отправителя. За nginx/Caddy на этом же сервере берём его из X-Forwarded-For
    (последний адрес — тот, что добавил наш прокси), иначе — адрес соединения."""
    remote = request.remote or ""
    try:
        local = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        local = False
    if local:
        forwarded = request.headers.get("X-Forwarded-For", "") or request.headers.get("X-Real-IP", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return remote

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # страница оферты — статичный HTML со встроенными стилями, скрипты и внешние ресурсы не нужны
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        e.headers.update(SECURITY_HEADERS)
        raise
    resp.headers.update(SECURITY_HEADERS)
    return resp


def make_app(shop: Shop) -> web.Application:
    last_alert: dict[str, float | None] = {"t": None}  # None — тревог ещё не было

    def bot_link(payload: str) -> str:
        return f"https://t.me/{shop.bot_username}?start={payload}" if shop.bot_username else "https://t.me"

    async def alert(text: str) -> None:
        now = time.monotonic()
        if last_alert["t"] is None or now - last_alert["t"] > ALERT_EVERY_SEC:
            last_alert["t"] = now
            await shop._notify_admins(text)

    async def notify(request: web.Request) -> web.Response:
        if not shop.cfg.payments_enabled:
            return web.Response(status=503, text="payments disabled")
        ip = client_ip(request)
        if shop.cfg.check_ip and not is_yookassa_ip(ip):
            # Даже без этой проверки подделка ничего бы не дала (платёж перепроверяется через API),
            # но лишние запросы к ЮKassa не нужны
            log.warning("Уведомление с чужого адреса %s отклонено", ip)
            await alert(texts.ADMIN_FOREIGN_NOTIFICATION.format(ip=ip))
            return web.Response(status=403, text="forbidden")
        try:
            data = json.loads(await request.read())  # размер ограничен client_max_size
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400, text="bad json")
        obj = data.get("object") if isinstance(data, dict) else None
        event = str(data.get("event", "")) if isinstance(data, dict) else ""
        if not isinstance(obj, dict) or data.get("type") != "notification":
            return web.Response(status=400, text="not a notification")
        if event in PAYMENT_EVENTS:
            payment_id = obj.get("id")
        elif event in REFUND_EVENTS:
            payment_id = obj.get("payment_id")
        else:
            log.info("Уведомление ЮKassa %s — не обрабатывается", event)
            return web.Response(text="ok")
        if not isinstance(payment_id, str) or not OBJECT_ID_RE.match(payment_id):
            return web.Response(status=400, text="bad object id")
        try:
            result = await shop.handle_notification(event, payment_id)
        except YooKassaError as e:
            log.warning("Уведомление %s по платежу %s: ЮKassa недоступна (%s) — ЮKassa повторит", event, payment_id, e)
            return web.Response(status=503, text="retry later")
        except Exception:
            log.exception("Ошибка обработки уведомления %s по платежу %s", event, payment_id)
            return web.Response(status=500, text="error")  # ЮKassa повторит уведомление
        log.info("Уведомление %s по платежу %s: %s", event, payment_id, result)
        return web.Response(text="ok")

    async def success(_: web.Request) -> web.Response:
        raise web.HTTPFound(bot_link("paid"))

    async def back(_: web.Request) -> web.Response:
        raise web.HTTPFound(bot_link("fail"))

    async def offer(_: web.Request) -> web.Response:
        return web.Response(text=render_offer(shop.cfg, shop.bot_username), content_type="text/html")

    async def health(_: web.Request) -> web.Response:
        # 503, если база недоступна — так мониторинг заметит проблему раньше покупателей
        if not await shop.db.ping():
            return web.Response(status=503, text="database unavailable")
        return web.Response(text="ok")

    # уведомления ЮKassa — несколько килобайт; большие тела отклоняем сразу
    app = web.Application(middlewares=[security_headers], client_max_size=256 * 1024)
    app.router.add_post("/pay/notify", notify)
    app.router.add_route("*", "/pay/success", success)
    app.router.add_route("*", "/pay/return", back)
    app.router.add_get("/offer", offer)
    app.router.add_get("/health", health)
    return app
