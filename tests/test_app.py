"""Интеграционный тест: настоящее приложение (bot.main.run) целиком — веб-сервер для Робокассы,
фоновые задачи и обработчики — против поддельного сервера Telegram Bot API.
Сценарий ровно как у покупателя: /start → «Купить» → оплата → уведомление Робокассы → гайд."""
import asyncio
import hashlib
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

LOGIN, P1, P2 = "aibezlesa", "p1$x", "p2${HOME}"   # «$» в паролях — частая ловушка
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
    cfg.rk_login, cfg.rk_password1, cfg.rk_password2, cfg.rk_test, cfg.rk_hash = LOGIN, P1, P2, True, "md5"
    cfg.admin_ids = [999]
    cfg.rk_test_public = True
    cfg.web_enabled, cfg.web_host, cfg.web_port = True, "127.0.0.1", free_port()
    cfg.public_url, cfg.offer_url = "https://example.test", "https://example.test/offer"
    cfg.product_file, cfg.db_path = sample_pdf, tmp_path / "bot.db"
    cfg.seller_name, cfg.seller_inn, cfg.seller_email, cfg.support_username = "Тест Тестович", "123456789012", "a@b.c", "help"
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


def notify(inv: str, password: str = P2, out_sum: str = "890.000000") -> dict:
    sig = hashlib.md5(f"{out_sum}:{inv}:{password}:Shp_user=111".encode()).hexdigest().upper()
    return {"OutSum": out_sum, "InvId": inv, "Shp_user": "111", "SignatureValue": sig, "IsTest": "1"}


async def buy(tg: FakeTelegram) -> str:
    tg.push("/start tiktok")
    await wait_for(lambda: any("ИИ без тёмного леса" in (d.get("text") or "") for d in tg.sent("sendMessage")))
    await asyncio.sleep(0.6)  # живой человек не нажимает чаще, чем раз в полсекунды
    tg.push(callback="buy")
    await wait_for(lambda: any("robokassa" in json.dumps(d.get("reply_markup") or {}) for d in tg.sent("sendMessage")))
    pay = [d for d in tg.sent("sendMessage") if "robokassa" in json.dumps(d.get("reply_markup") or {})][-1]
    markup = pay["reply_markup"] if isinstance(pay["reply_markup"], dict) else json.loads(pay["reply_markup"])
    q = {k: v[0] for k, v in parse_qs(urlparse(markup["inline_keyboard"][0][0]["url"]).query).items()}
    assert q["SignatureValue"] == hashlib.md5(f"{LOGIN}:890.00:{q['InvId']}:{P1}:Shp_user=111".encode()).hexdigest()
    return q["InvId"]


async def test_purchase_end_to_end(app):
    tg, http, base, task = app
    await wait_for(lambda: tg.sent("setMyCommands"))
    assert any("ТЕСТОВЫЙ" in d["text"] for d in tg.sent("sendMessage") if str(d["chat_id"]) == "999")
    inv = await buy(tg)

    forged = await http.post(f"{base}/robokassa/result", data=notify(inv, password="guess"))
    assert forged.status == 400
    r = await http.post(f"{base}/robokassa/result", data=notify(inv))
    assert r.status == 200 and await r.text() == f"OK{inv}"
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1)
    again = await http.post(f"{base}/robokassa/result", data=notify(inv))   # Робокасса повторила уведомление
    assert again.status == 200
    await asyncio.sleep(1.5)
    assert len(tg.sent("sendDocument")) == 1
    assert any("Новая продажа" in d["text"] for d in tg.sent("sendMessage") if str(d["chat_id"]) == "999")

    succ = await http.get(f"{base}/robokassa/success", allow_redirects=False)
    assert succ.headers["Location"] == "https://t.me/guide_bot?start=paid"
    tg.push("/start paid")
    await wait_for(lambda: "уже отправлен" in (tg.sent("sendMessage")[-1].get("text") or ""))
    assert len(tg.sent("sendDocument")) == 1

    offer = await http.get(f"{base}/offer")
    body = await offer.text()
    assert offer.status == 200 and "Тест Тестович" in body and "@guide_bot" in body and "НЕ ЗАПОЛНЕНО" not in body
    assert (await http.get(f"{base}/health")).status == 200


async def test_payment_is_not_lost_while_telegram_is_down(app):
    """Telegram недоступен в момент оплаты: Робокасса получает OK, гайд приходит, когда связь вернулась."""
    tg, http, base, task = app
    inv = await buy(tg)
    tg.down = True
    r = await http.post(f"{base}/robokassa/result", data=notify(inv))
    assert r.status == 200 and await r.text() == f"OK{inv}"
    await asyncio.sleep(1)
    assert tg.sent("sendDocument") == []
    tg.down = False
    await wait_for(lambda: len(tg.sent("sendDocument")) == 1, timeout=25)


async def test_webhook_works_before_telegram_is_reachable(tmp_path, sample_pdf):
    """Если при запуске Telegram недоступен, адреса для Робокассы всё равно отвечают."""
    cfg = Config()
    cfg.bot_token = "123456:" + "A" * 35
    cfg.rk_login, cfg.rk_password1, cfg.rk_password2 = LOGIN, P1, P2
    cfg.web_host, cfg.web_port = "127.0.0.1", free_port()
    cfg.public_url, cfg.product_file, cfg.db_path = "", sample_pdf, tmp_path / "bot.db"
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
