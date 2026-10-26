"""Интеграционный тест: настоящее приложение (bot.main.run) целиком — веб-сервер для Робокассы,
фоновые задачи и обработчики — против поддельного сервера Telegram Bot API.
Сценарий ровно как у покупателя: /start → «Купить» → оплата → уведомление Prodamus → гайд."""
import asyncio
import json
import socket
from urllib.parse import parse_qs, urlparse

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import ClientSession, web

import bot.handlers as handlers
from bot.config import Config
from bot.main import run
from bot.prodamus import sign
from tests.test_shop import form_body, notification

SECRET = "k$ey${HOME}/x"   # «$» и «/» в секретном ключе — частые ловушки
FORM = "https://aibezlesa.payform.ru"
USER = {"id": 111, "is_bot": False, "first_name": "Иван", "username": "ivan"}
CHAT = {"id": 111, "type": "private"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeTelegram:
    """Минимальный Bot API: getMe, getUpdates, sendMessage, sendDocument и т.п."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.down = False
        self._uid = 0

    def push(self, text: str | None = None, callback: str | None = None) -> None:
        self._uid += 1
        if text is not None:
            ent = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
            self.updates.append({"update_id": self._uid, "message": {
                "message_id": self._uid, "date": 0, "chat": CHAT, "from": USER, "text": text, "entities": ent}})
        else:
            self.updates.append({"update_id": self._uid, "callback_query": {
                "id": str(self._uid), "from": USER, "chat_instance": "c", "data": callback,
                "message": {"message_id": 1, "date": 0, "chat": CHAT, "text": "x"}}})

    def sent(self, method: str) -> list[dict]:
        return [d for m, d in self.calls if m == method]

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        if self.down:
            raise web.HTTPBadGateway()
        if request.content_type == "application/json":
            data = await request.json()
        else:
            data = {k: (v if isinstance(v, str) else "<file>") for k, v in (await request.post()).items()}
        if method == "getUpdates":
            await asyncio.sleep(0.05)
            result, self.updates = self.updates, []
            return web.json_response({"ok": True, "result": result})
        self.calls.append((method, data))
        if method == "getMe":
            result = {"id": 42, "is_bot": True, "first_name": "Guide", "username": "guide_bot"}
        elif method == "sendMessage":
            result = {"message_id": 500, "date": 0, "chat": {"id": int(data["chat_id"]), "type": "private"},
                      "text": data.get("text")}
        elif method == "sendDocument":
            result = {"message_id": 501, "date": 0, "chat": {"id": int(data["chat_id"]), "type": "private"},
                      "document": {"file_id": "FID", "file_unique_id": "U"}}
        else:
            result = True
        return web.json_response({"ok": True, "result": result})


async def wait_for(cond, timeout: float = 10) -> None:
    for _ in range(int(timeout * 20)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("не дождались")


@pytest.fixture
async def app(tmp_path, sample_pdf, monkeypatch):
    monkeypatch.setattr(handlers, "RETURN_CHECK_PAUSE", 0.05)
    handlers._last_sent.clear()
    tg = FakeTelegram()
    tg_app = web.Application()
    tg_app.router.add_route("*", "/bot{token}/{method}", tg.handle)
    tg_runner = web.AppRunner(tg_app)
    await tg_runner.setup()
    tg_port = free_port()
    await web.TCPSite(tg_runner, "127.0.0.1", tg_port).start()

    cfg = Config()
    cfg.bot_token = "123456:" + "A" * 35
    cfg.pay_url, cfg.pay_secret, cfg.test_mode = FORM, SECRET, False
    cfg.admin_ids = [999]
    cfg.web_enabled, cfg.web_host, cfg.web_port = True, "127.0.0.1", free_port()
    cfg.public_url, cfg.offer_url = "https://example.test", "https://example.test/offer"
    cfg.product_file, cfg.db_path = sample_pdf, tmp_path / "bot.db"
    cfg.seller_name, cfg.seller_inn, cfg.seller_email, cfg.support_username = "Малютин Максим", "701760448883", "a@b.c", "help"
    cfg.poll_interval_sec = 1

    session = AiohttpSession(api=TelegramAPIServer.from_base(f"http://127.0.0.1:{tg_port}"))
    task = asyncio.create_task(run(cfg, session=session))
    base = f"http://127.0.0.1:{cfg.web_port}"
    async with ClientSession() as http:
        yield tg, http, base, task
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task
    await tg_runner.cleanup()
    handlers.router._parent_router = None


async def post_notification(http, base, order, secret=SECRET, total="890.00"):
    data = notification(order, total=total)
    return await http.post(f"{base}/pay/notify", data=form_body(data),
                           headers={"Content-Type": "application/x-www-form-urlencoded", "Sign": sign(data, secret)})


async def buy(tg: FakeTelegram) -> str:
    tg.push("/start tiktok")
    await wait_for(lambda: any("ИИ без тёмного леса" in (d.get("text") or "") for d in tg.sent("sendMessage")))
    await asyncio.sleep(0.6)  # живой человек не нажимает чаще, чем раз в полсекунды
    tg.push(callback="buy")
    await wait_for(lambda: any("payform.ru" in json.dumps(d.get("reply_markup") or {}) for d in tg.sent("sendMessage")))
    pay = [d for d in tg.sent("sendMessage") if "payform.ru" in json.dumps(d.get("reply_markup") or {})][-1]
    markup = pay["reply_markup"] if isinstance(pay["reply_markup"], dict) else json.loads(pay["reply_markup"])
    q = {k: v[0] for k, v in parse_qs(urlparse(markup["inline_keyboard"][0][0]["url"]).query).items()}
    assert q["do"] == "pay" and q["products[0][price]"] == "890.00"
    return q["order_id"]


async def test_purchase_end_to_end(app):
    tg, http, base, task = app
    await wait_for(lambda: tg.sent("setMyShortDescription"))
    assert "ИНН 701760448883" in tg.sent("setMyShortDescription")[0]["short_description"]
    assert "Малютин Максим" in tg.sent("setMyDescription")[0]["description"]
    assert any("/pay/notify" in d["text"] for d in tg.sent("sendMessage") if str(d["chat_id"]) == "999")
    order = await buy(tg)

    forged = await post_notification(http, base, order, secret="guess")
    assert forged.status == 400
    r = await post_notification(http, base, order)
    assert r.status == 200 and await r.text() == "success"
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1)
    again = await post_notification(http, base, order)   # Prodamus повторил уведомление
    assert again.status == 200
    await asyncio.sleep(1.5)
    assert len(tg.sent("sendDocument")) == 1
    assert any("Новая продажа" in d["text"] for d in tg.sent("sendMessage") if str(d["chat_id"]) == "999")

    succ = await http.get(f"{base}/pay/success", allow_redirects=False)
    assert succ.headers["Location"] == "https://t.me/guide_bot?start=paid"
    tg.push("/start paid")
    await wait_for(lambda: "уже отправлен" in (tg.sent("sendMessage")[-1].get("text") or ""))
    assert len(tg.sent("sendDocument")) == 1

    offer = await http.get(f"{base}/offer")
    body = await offer.text()
    assert offer.status == 200 and "Малютин Максим" in body and "@guide_bot" in body and "НЕ ЗАПОЛНЕНО" not in body
    assert (await http.get(f"{base}/health")).status == 200


async def test_return_before_notification(app):
    """Покупатель нажал «Вернуться в магазин» раньше, чем пришло уведомление: бот ждёт и выдаёт гайд."""
    tg, http, base, task = app
    order = await buy(tg)
    await asyncio.sleep(0.6)
    tg.push("/start paid")
    await wait_for(lambda: any("Проверяю оплату" in (d.get("text") or "") for d in tg.sent("sendMessage")))
    r = await post_notification(http, base, order)
    assert r.status == 200
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1)
    await asyncio.sleep(0.5)
    assert not any("пока не поступила" in (d.get("text") or "") for d in tg.sent("sendMessage"))


async def test_payment_is_not_lost_while_telegram_is_down(app):
    """Telegram недоступен в момент оплаты: Prodamus получает ответ, гайд приходит, когда связь вернулась."""
    tg, http, base, task = app
    order = await buy(tg)
    tg.down = True
    r = await post_notification(http, base, order)
    assert r.status == 200 and await r.text() == "success"
    await asyncio.sleep(1)
    assert tg.sent("sendDocument") == []
    tg.down = False
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1, timeout=25)


async def test_webhook_works_before_telegram_is_reachable(tmp_path, sample_pdf):
    """Если при запуске Telegram недоступен, приём уведомлений об оплате всё равно работает."""
    cfg = Config()
    cfg.bot_token = "123456:" + "A" * 35
    cfg.pay_url, cfg.pay_secret = FORM, SECRET
    cfg.web_host, cfg.web_port = "127.0.0.1", free_port()
    cfg.public_url, cfg.product_file, cfg.db_path = "https://example.test", sample_pdf, tmp_path / "bot.db"
    dead = AiohttpSession(api=TelegramAPIServer.from_base(f"http://127.0.0.1:{free_port()}"))
    task = asyncio.create_task(run(cfg, session=dead))
    try:
        async with ClientSession() as http:
            for _ in range(100):
                try:
                    r = await http.get(f"http://127.0.0.1:{cfg.web_port}/health")
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            assert r.status == 200
    finally:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task
        handlers.router._parent_router = None
