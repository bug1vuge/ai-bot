"""Веб-адреса для Робокассы:
  POST /robokassa/result   — уведомление об оплате (Result URL)
  GET  /robokassa/success  — сюда Робокасса возвращает покупателя после оплаты (Success URL)
  GET  /robokassa/fail     — сюда возвращает при отказе от оплаты (Fail URL)
  GET  /offer              — публичная оферта (текст в data/offer.md)
  GET  /health             — проверка, что бот жив
"""
import logging

from aiohttp import web

from .offer import render_offer
from .service import Shop

log = logging.getLogger(__name__)


async def _params(request: web.Request) -> dict:
    data = dict(request.query)
    if request.method == "POST":
        try:
            data.update(dict(await request.post()))
        except web.HTTPException:
            raise  # например, слишком большое тело запроса — 413
        except Exception:
            log.warning("Не удалось разобрать тело запроса от %s", request.remote)
    return {k: str(v) for k, v in data.items()}


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
    def bot_link(payload: str) -> str:
        return f"https://t.me/{shop.bot_username}?start={payload}" if shop.bot_username else "https://t.me"

    async def result(request: web.Request) -> web.Response:
        data = await _params(request)
        if not shop.cfg.payments_enabled:
            return web.Response(status=503, text="payments disabled")
        if not shop.kassa.check_result_signature(data):
            log.warning("Неверная подпись уведомления от %s: InvId=%s", request.remote, data.get("InvId"))
            return web.Response(status=400, text="bad sign")
        try:
            ok = await shop.handle_result(data)
        except Exception:
            log.exception("Ошибка обработки уведомления %s", data.get("InvId"))
            return web.Response(status=500, text="error")  # Робокасса повторит уведомление
        if not ok:
            return web.Response(status=400, text="rejected")
        return web.Response(text=f"OK{data['InvId']}")

    async def success(_: web.Request) -> web.Response:
        raise web.HTTPFound(bot_link("paid"))

    async def fail(_: web.Request) -> web.Response:
        raise web.HTTPFound(bot_link("fail"))

    async def offer(_: web.Request) -> web.Response:
        return web.Response(text=render_offer(shop.cfg, shop.bot_username), content_type="text/html")

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    # уведомления Робокассы — несколько сотен байт; большие тела отклоняем сразу
    app = web.Application(middlewares=[security_headers], client_max_size=64 * 1024)
    app.router.add_route("*", "/robokassa/result", result)
    app.router.add_route("*", "/robokassa/success", success)
    app.router.add_route("*", "/robokassa/fail", fail)
    app.router.add_get("/offer", offer)
    app.router.add_get("/health", health)
    return app
