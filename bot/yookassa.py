"""Клиент API ЮKassa (https://api.yookassa.ru/v3).

Принципы:
- Источник правды — только API. Уведомлению ЮKassa бот не верит на слово: из него берётся ID
  платежа, а статус, сумма, магазин и метаданные всегда перечитываются запросом к API.
- Каждый изменяющий запрос (создание платежа, подтверждение, возврат) идёт с ключом
  идемпотентности. Повтор после обрыва связи возвращает тот же результат, а не создаёт дубль.
- Сетевые ошибки, 5xx, 429 и 202 («запрос в обработке») повторяются с паузой; ошибки в данных
  (400, 401, 403, 404) не повторяются.
"""
import asyncio
import base64
import ipaddress
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://api.yookassa.ru/v3"

# Адреса, с которых ЮKassa отправляет уведомления (документация ЮKassa, раздел «Входящие уведомления»)
YOOKASSA_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25", "77.75.156.11/32",
    "77.75.156.35/32", "77.75.154.128/25", "2a02:5180::/32",
))

# ID объектов ЮKassa — UUID-подобная строка (у платежей вторая группа нестандартная)
OBJECT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

PENDING, WAITING, SUCCEEDED, CANCELED = "pending", "waiting_for_capture", "succeeded", "canceled"


class YooKassaError(Exception):
    """Ошибка API. status — HTTP-код (0 — нет связи), code — код ошибки ЮKassa."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def not_found(self) -> bool:
        return self.status == 404

    @property
    def temporary(self) -> bool:
        return self.status == 0 or self.status >= 500 or self.status in (202, 429)


def is_yookassa_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in YOOKASSA_NETWORKS if net.version == addr.version)


def money(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


@dataclass
class Payment:
    """То, что боту нужно знать о платеже ЮKassa."""
    id: str
    status: str
    paid: bool
    amount: Decimal | None
    currency: str
    test: bool
    metadata: dict
    confirmation_url: str
    refunded: Decimal
    account_id: str
    cancellation_reason: str

    @classmethod
    def parse(cls, data: dict) -> "Payment":
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise YooKassaError("в ответе ЮKassa нет платежа")
        amount = data.get("amount") or {}
        refunded = (data.get("refunded_amount") or {}).get("value")
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        return cls(
            id=data["id"],
            status=str(data.get("status") or ""),
            paid=data.get("paid") is True,
            amount=money(amount.get("value")),
            currency=str(amount.get("currency") or ""),
            test=data.get("test") is True,
            metadata={str(k): str(v) for k, v in meta.items()},
            confirmation_url=str((data.get("confirmation") or {}).get("confirmation_url") or ""),
            refunded=money(refunded) or Decimal("0.00"),
            account_id=str((data.get("recipient") or {}).get("account_id") or ""),
            cancellation_reason=str((data.get("cancellation_details") or {}).get("reason") or ""),
        )


class YooKassa:
    def __init__(self, shop_id: str, secret_key: str, api_url: str = API_URL, *,
                 timeout: float = 15, attempts: int = 4, backoff: float = 1.0):
        self.shop_id = shop_id
        self.api_url = api_url.rstrip("/")
        token = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._attempts = attempts
        self._backoff = backoff
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _request(self, method: str, path: str, *, json: dict | None = None,
                       idempotence_key: str | None = None) -> dict:
        headers = {"Content-Type": "application/json", "Authorization": self._auth_header}
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key[:64]
        last: YooKassaError | None = None
        for attempt in range(1, self._attempts + 1):
            wait = self._backoff * 2 ** (attempt - 1)
            try:
                async with self._http().request(method, self.api_url + path, json=json, headers=headers) as resp:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        body = {}
                    if resp.status == 200:
                        return body if isinstance(body, dict) else {}
                    code = str(body.get("code", "")) if isinstance(body, dict) else ""
                    desc = str(body.get("description", "")) if isinstance(body, dict) else ""
                    last = YooKassaError(f"ЮKassa {resp.status} {code}: {desc}".strip(), resp.status, code)
                    if resp.status == 202 and isinstance(body, dict):
                        # запрос принят, но ещё обрабатывается — повторить с тем же ключом через retry_after мс
                        wait = max(wait, min(float(body.get("retry_after", 1000)) / 1000, 30))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = YooKassaError(f"нет связи с ЮKassa: {e.__class__.__name__} {e}".strip())
            if not last.temporary:
                raise last
            # POST без ключа идемпотентности повторять нельзя: можно создать дубль
            if method != "GET" and not idempotence_key:
                raise last
            if attempt < self._attempts:
                log.warning("%s %s: %s — повтор через %.1f с", method, path, last, wait)
                await asyncio.sleep(wait)
        raise last  # type: ignore[misc]

    async def create_payment(self, *, order_id: str, user_id: int, amount: str, description: str,
                             return_url: str, idempotence_key: str) -> Payment:
        body = {
            "amount": {"value": amount, "currency": "RUB"},
            "capture": True,  # одностадийный платёж: деньги списываются сразу
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": {"order_id": order_id, "user_id": str(user_id)},
        }
        return Payment.parse(await self._request("POST", "/payments", json=body, idempotence_key=idempotence_key))

    async def get_payment(self, payment_id: str) -> Payment:
        if not OBJECT_ID_RE.match(payment_id):
            raise YooKassaError(f"некорректный ID платежа: {payment_id!r}", 400, "invalid_id")
        return Payment.parse(await self._request("GET", f"/payments/{payment_id}"))

    async def capture(self, payment: Payment) -> Payment:
        """Подтвердить платёж в статусе waiting_for_capture (на случай, если магазин настроен на двухстадийные платежи)."""
        body = {"amount": {"value": f"{payment.amount}", "currency": payment.currency or "RUB"}}
        return Payment.parse(await self._request("POST", f"/payments/{payment.id}/capture", json=body,
                                                 idempotence_key=f"capture-{payment.id}"))

    async def refund(self, payment: Payment, reason: str) -> dict:
        """Полный возврат платежа. Ключ идемпотентности привязан к платежу — второй возврат невозможен."""
        body = {"payment_id": payment.id, "amount": {"value": f"{payment.amount}", "currency": payment.currency or "RUB"},
                "description": reason[:250]}
        return await self._request("POST", "/refunds", json=body, idempotence_key=f"refund-{payment.id}")
