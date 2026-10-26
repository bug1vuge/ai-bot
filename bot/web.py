"""Веб-адреса бота:
  POST /pay/notify   — уведомление Prodamus об оплате (вписывается в настройки платёжной страницы)
  GET  /pay/success  — сюда Prodamus возвращает покупателя после оплаты → обратно в бота
  GET  /pay/return   — сюда возвращает, если покупатель ушёл без оплаты → обратно в бота
  GET  /offer        — публичная оферта (текст в data/offer.md)
  GET  /health       — проверка, что бот жив
"""
import html
import logging
import time
from datetime import datetime, timezone

from aiohttp import web

from . import texts
from .offer import render_offer
from .prodamus import ProdamusError, notification_fields
from .service import Shop

log = logging.getLogger(__name__)

# Уведомления с неверной подписью сохраняются для разбора (не больше стольких файлов)
KEEP_REJECTED = 50
# Администратору о неверных подписях — не чаще раза в столько секунд
ALERT_EVERY_SEC = 600

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
    last_alert = {"t": 0.0}

    def bot_link(payload: str) -> str:
        return f"https://t.me/{shop.bot_username}?start={payload}" if shop.bot_username else "https://t.me"

    def keep_rejected(body: bytes, reason: str) -> None:
        """Сохраняем отклонённое уведомление: если это настоящая оплата, по файлу можно разобраться."""
        folder = shop.cfg.db_path.parent / "rejected_notifications"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            (folder / f"{stamp}.txt").write_bytes(f"# {reason}\n".encode() + body)
            for old in sorted(folder.glob("*.txt"))[:-KEEP_REJECTED]:
                old.unlink(missing_ok=True)
        except OSError:
            log.exception("Не удалось сохранить отклонённое уведомление")

    async def notify(request: web.Request) -> web.Response:
        if not shop.cfg.payments_enabled:
            return web.Response(status=503, text="payments disabled")
        body = await request.read()  # размер ограничен client_max_size
        try:
            data = shop.pay.parse_notification(body, request.content_type, request.headers.get("Sign", ""))
        except (ProdamusError, UnicodeDecodeError) as e:
            log.warning("Отклонено уведомление от %s: %s", request.remote, e)
            keep_rejected(body, str(e))
            now = time.monotonic()
            if now - last_alert["t"] > ALERT_EVERY_SEC:
                last_alert["t"] = now
                await shop._notify_admins(texts.ADMIN_BAD_SIGNATURE.format(reason=html.escape(str(e))))
            return web.Response(status=400, text=f"error: {e}")
        try:
            await shop.handle_notification(data)
        except Exception:
            log.exception("Ошибка обработки уведомления по заказу %s", notification_fields(data)["order"])
            return web.Response(status=500, text="error")  # Prodamus повторит уведомление
        return web.Response(text="success")

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

    # уведомления об оплате — несколько килобайт; большие тела отклоняем сразу
    app = web.Application(middlewares=[security_headers], client_max_size=256 * 1024)
    app.router.add_post("/pay/notify", notify)
    app.router.add_route("*", "/pay/success", success)
    app.router.add_route("*", "/pay/return", back)
    app.router.add_get("/offer", offer)
    app.router.add_get("/health", health)
    return app
