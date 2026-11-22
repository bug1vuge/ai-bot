"""Поддельный API ЮKassa для тестов: настоящий HTTP-сервер с проверкой авторизации,
идемпотентностью, подтверждением и возвратами, плюс управляемые сбои."""
import asyncio
import base64
import json
import uuid
from decimal import Decimal

from aiohttp import web

SHOP_ID = "123456"
SECRET = "live_" + "S" * 40


class FakeYooKassa:
    def __init__(self, shop_id: str = SHOP_ID, secret: str = SECRET):
        self.shop_id, self.secret = shop_id, secret
        self.payments: dict[str, dict] = {}
        self.idempotent: dict[str, tuple[str, dict]] = {}   # ключ → (тело запроса, ответ)
        self.calls: list[tuple[str, str, dict | None]] = []
        # (префикс «METHOD /path», HTTP-статус или "slow": запрос выполняется, но ответ приходит
        # позже таймаута клиента — как при обрыве связи после того, как ЮKassa уже создала платёж)
        self.failures: list[tuple[str, object]] = []
        self.slow_delay = 2.0
        self.test_payments = False
        self.created = 0
        self.refunds = 0
        self.account: dict = {}   # поля ответа GET /me поверх стандартных

    # ---------- управление ----------
    def fail_next(self, prefix: str, what: object, times: int = 1) -> None:
        self.failures += [(prefix, what)] * times

    def pay(self, pid: str, **changes) -> None:
        self.payments[pid].update({"status": "succeeded", "paid": True, **changes})

    def wait_capture(self, pid: str) -> None:
        self.payments[pid].update({"status": "waiting_for_capture", "paid": True})

    def cancel(self, pid: str) -> None:
        self.payments[pid].update({"status": "canceled", "paid": False,
                                   "cancellation_details": {"party": "yoo_money", "reason": "expired_on_confirmation"}})

    def refund_all(self, pid: str) -> None:
        self.payments[pid]["refunded_amount"] = dict(self.payments[pid]["amount"])

    def by_order(self, order_id: str) -> dict:
        return next(p for p in self.payments.values() if p["metadata"].get("order_id") == order_id)

    def count(self, prefix: str) -> int:
        return sum(1 for m, path, _ in self.calls if f"{m} {path}".startswith(prefix))

    # ---------- сервер ----------
    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/v3/{tail:.*}", self.handle)
        return app

    async def handle(self, request: web.Request) -> web.StreamResponse:
        method, path = request.method, "/" + request.match_info["tail"]
        body = await request.json() if request.can_read_body else None
        self.calls.append((method, path, body))
        slow = False
        for i, (prefix, what) in enumerate(self.failures):
            if f"{method} {path}".startswith(prefix):
                del self.failures[i]
                if what == "slow":
                    slow = True
                    break
                if what == 202:
                    return web.json_response({"type": "processing", "retry_after": 10}, status=202)
                return web.json_response({"type": "error", "code": "internal_server_error"}, status=what)
        response = await self._dispatch(request, method, path, body)
        if slow:
            await asyncio.sleep(self.slow_delay)
        return response

    async def _dispatch(self, request, method, path, body) -> web.Response:
        auth = request.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{self.shop_id}:{self.secret}".encode()).decode()
        if auth != expected:
            return web.json_response({"type": "error", "code": "invalid_credentials"}, status=401)

        if method == "POST":
            key = request.headers.get("Idempotence-Key")
            if not key:
                return web.json_response({"type": "error", "code": "invalid_request"}, status=400)
            if key in self.idempotent:
                saved_body, saved = self.idempotent[key]
                if saved_body != json.dumps(body, sort_keys=True):
                    return web.json_response({"type": "error", "code": "invalid_request",
                                              "description": "Idempotence key duplicated"}, status=400)
                return web.json_response(self._current(saved))
            result = self._post(path, body)
            if isinstance(result, web.Response):
                return result
            self.idempotent[key] = (json.dumps(body, sort_keys=True), result)
            return web.json_response(result)

        if method == "GET" and path == "/me":
            return web.json_response({"account_id": self.shop_id, "test": self.test_payments, "status": "enabled",
                                      "fiscalization_enabled": False, **self.account})
        if method == "GET" and path.startswith("/payments/"):
            p = self.payments.get(path.split("/")[2])
            if p is None:
                return web.json_response({"type": "error", "code": "not_found"}, status=404)
            return web.json_response(p)
        return web.json_response({"type": "error", "code": "not_found"}, status=404)

    def _current(self, saved: dict) -> dict:
        """Повтор запроса с тем же ключом: для платежа — его текущее состояние, для возврата — тот же ответ."""
        return self.payments.get(saved.get("id"), saved)

    def _post(self, path: str, body: dict):
        if path == "/payments":
            self.created += 1
            pid = str(uuid.uuid4())
            self.payments[pid] = {
                "id": pid, "status": "pending", "paid": False, "amount": body["amount"],
                "description": body.get("description"), "metadata": body.get("metadata", {}),
                "recipient": {"account_id": self.shop_id, "gateway_id": "1"},
                "confirmation": {"type": "redirect", "confirmation_url": f"https://yoomoney.test/checkout?orderId={pid}",
                                 "return_url": body["confirmation"]["return_url"]},
                "test": self.test_payments, "refundable": False, "created_at": "2026-11-21T10:00:00.000Z",
            }
            return self.payments[pid]
        if path.startswith("/payments/") and path.endswith("/capture"):
            p = self.payments.get(path.split("/")[2])
            if p is None:
                return web.json_response({"type": "error", "code": "not_found"}, status=404)
            if p["status"] == "waiting_for_capture":
                p["status"] = "succeeded"
            return p
        if path == "/refunds":
            p = self.payments.get(body["payment_id"])
            if p is None or p["status"] != "succeeded":
                return web.json_response({"type": "error", "code": "invalid_request"}, status=400)
            self.refunds += 1
            refunded = Decimal(p.get("refunded_amount", {}).get("value", "0")) + Decimal(body["amount"]["value"])
            p["refunded_amount"] = {"value": f"{refunded:.2f}", "currency": "RUB"}
            return {"id": str(uuid.uuid4()), "payment_id": p["id"], "status": "succeeded", "amount": body["amount"]}
        return web.json_response({"type": "error", "code": "not_found"}, status=404)
