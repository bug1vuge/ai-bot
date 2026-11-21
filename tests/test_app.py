"""Интеграционный тест: настоящее приложение (bot.main.run) целиком — веб-сервер для Робокассы,
фоновые задачи и обработчики — против поддельного сервера Telegram Bot API.
Сценарий ровно как у покупателя: /start → «Купить» → оплата → уведомление ЮKassa → гайд."""
import asyncio
import json
import socket

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import ClientSession, web

import bot.handlers as handlers
from bot.config import Config
from bot.main import run
from tests.fake_yookassa import SECRET, SHOP_ID

YK_IP = {"X-Forwarded-For": "185.71.76.5"}
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
async def app(tmp_path, sample_pdf, monkeypatch, yk):
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
    cfg.shop_id, cfg.secret_key, cfg.api_url, cfg.test_mode = SHOP_ID, SECRET, yk.url, False
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
        http.yk = yk
        yield tg, http, base, task
    task.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await task
    await tg_runner.cleanup()
    handlers.router._parent_router = None


async def post_notification(http, base, order, event="payment.succeeded", headers=YK_IP):
    pid = http.yk.by_order(order)["id"]
    body = {"type": "notification", "event": event, "object": {"id": pid, "status": "succeeded", "paid": True}}
    return await http.post(f"{base}/pay/notify", json=body, headers=headers)


async def buy(tg: FakeTelegram) -> str:
    tg.push("/start tiktok")
    await wait_for(lambda: any("ИИ без тёмного леса" in (d.get("text") or "") for d in tg.sent("sendMessage")))
    await asyncio.sleep(0.6)  # живой человек не нажимает чаще, чем раз в полсекунды
    tg.push(callback="buy")
    await wait_for(lambda: any("yoomoney.test" in json.dumps(d.get("reply_markup") or {}) for d in tg.sent("sendMessage")))
    pay = [d for d in tg.sent("sendMessage") if "yoomoney.test" in json.dumps(d.get("reply_markup") or {})][-1]
    markup = pay["reply_markup"] if isinstance(pay["reply_markup"], dict) else json.loads(pay["reply_markup"])
    return markup["inline_keyboard"][1][0]["callback_data"].split(":", 1)[1]


async def test_purchase_end_to_end(app):
    tg, http, base, task = app
    yk = http.yk
    await wait_for(lambda: tg.sent("setMyShortDescription"))
    assert "ИНН 701760448883" in tg.sent("setMyShortDescription")[0]["short_description"]
    assert "Малютин Максим" in tg.sent("setMyDescription")[0]["description"]
    commands = tg.sent("setMyCommands")[0]["commands"]
    commands = json.loads(commands) if isinstance(commands, str) else commands
    assert any(c["command"] == "privacy" for c in commands)
    assert any("/pay/notify" in d["text"] for d in tg.sent("sendMessage") if str(d["chat_id"]) == "999")
    order = await buy(tg)
    assert yk.by_order(order)["amount"] == {"value": "990.00", "currency": "RUB"}

    forged = await post_notification(http, base, order)        # «оплачено», но платёж не оплачен
    assert forged.status == 200
    foreign = await post_notification(http, base, order, headers={"X-Forwarded-For": "6.6.6.6"})
    assert foreign.status == 403
    await asyncio.sleep(1.5)
    assert tg.sent("sendDocument") == []

    yk.pay(yk.by_order(order)["id"])                           # покупатель оплатил
    r = await post_notification(http, base, order)
    assert r.status == 200
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1)
    again = await post_notification(http, base, order)          # ЮKassa повторила уведомление
    assert again.status == 200
    await asyncio.sleep(1.5)
    assert len(tg.sent("sendDocument")) == 1
    assert any("Новая продажа" in d["text"] for d in tg.sent("sendMessage") if str(d["chat_id"]) == "999")

    succ = await http.get(f"{base}/pay/success", allow_redirects=False)
    assert succ.headers["Location"] == "https://t.me/guide_bot?start=paid"
    await asyncio.sleep(0.6)
    tg.push("/start paid")
    await wait_for(lambda: "уже отправлен" in (tg.sent("sendMessage")[-1].get("text") or ""))
    await asyncio.sleep(0.6)
    tg.push(callback="buy")                                     # снова «Купить» — новых платежей нет
    await wait_for(lambda: "уже есть этот гайд" in (tg.sent("sendMessage")[-1].get("text") or ""))
    assert len(tg.sent("sendDocument")) == 1 and yk.created == 1

    offer = await http.get(f"{base}/offer")
    body = await offer.text()
    assert offer.status == 200 and "Малютин Максим" in body and "@guide_bot" in body and "НЕ ЗАПОЛНЕНО" not in body
    assert (await http.get(f"{base}/health")).status == 200


async def test_return_before_notification(app):
    """Покупатель вернулся из ЮKassa раньше уведомления: бот сам спрашивает ЮKassa и выдаёт гайд."""
    tg, http, base, task = app
    order = await buy(tg)
    http.yk.pay(http.yk.by_order(order)["id"])
    await asyncio.sleep(0.6)
    tg.push("/start paid")                                      # уведомления нет вовсе
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1)
    await asyncio.sleep(0.5)
    assert not any("пока не поступила" in (d.get("text") or "") for d in tg.sent("sendMessage"))


async def test_payment_without_notification_is_found_by_poller(app):
    tg, http, base, task = app
    order = await buy(tg)
    http.yk.pay(http.yk.by_order(order)["id"])
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1, timeout=15)


async def test_payment_is_not_lost_while_telegram_is_down(app):
    """Telegram недоступен в момент оплаты: ЮKassa получает ответ, гайд приходит, когда связь вернулась."""
    tg, http, base, task = app
    order = await buy(tg)
    tg.down = True
    http.yk.pay(http.yk.by_order(order)["id"])
    r = await post_notification(http, base, order)
    assert r.status == 200
    await asyncio.sleep(1)
    assert tg.sent("sendDocument") == []
    tg.down = False
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1, timeout=25)


async def test_webhook_works_before_telegram_is_reachable(tmp_path, sample_pdf, yk):
    """Если при запуске Telegram недоступен, приём уведомлений об оплате всё равно работает."""
    cfg = Config()
    cfg.bot_token = "123456:" + "A" * 35
    cfg.shop_id, cfg.secret_key, cfg.api_url = SHOP_ID, SECRET, yk.url
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
