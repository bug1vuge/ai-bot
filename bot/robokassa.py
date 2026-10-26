"""Работа с Робокассой: ссылка на оплату, проверка уведомлений, запрос статуса платежа."""
import hashlib
import hmac
import json
import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

import aiohttp

log = logging.getLogger(__name__)

PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
STATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"

STATE_PAID = 100      # операция завершена успешно
STATE_CANCELED = 10   # операция отменена, деньги не получены


class RobokassaError(Exception):
    pass


def _shp_part(shp: dict) -> list[str]:
    """Пользовательские параметры Shp_ — в алфавитном порядке, в виде «Shp_key=value»."""
    return [f"{k}={shp[k]}" for k in sorted(shp)]


class Robokassa:
    def __init__(self, login: str, password1: str, password2: str, is_test: bool = True, algo: str = "md5"):
        self.login = login
        self.password1 = password1
        self.password2 = password2
        self.is_test = is_test
        self.algo = algo
        self._session: aiohttp.ClientSession | None = None

    def sign(self, *parts: str) -> str:
        return hashlib.new(self.algo, ":".join(parts).encode("utf-8")).hexdigest()

    # ---------- ссылка на оплату ----------
    def payment_url(self, inv_id: int, out_sum: str, description: str, shp: dict,
                    receipt: dict | None = None) -> str:
        """Подпись: MerchantLogin:OutSum:InvId[:Receipt]:Пароль#1[:Shp_...]"""
        params = {
            "MerchantLogin": self.login,
            "OutSum": out_sum,
            "InvId": str(inv_id),
            "Description": description[:100],
            "Culture": "ru",
            "Encoding": "utf-8",
        }
        parts = [self.login, out_sum, str(inv_id)]
        if receipt is not None:
            # Receipt участвует в подписи в URL-кодированном виде и в ссылке кодируется ещё раз
            receipt_enc = quote(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")), safe="")
            parts.append(receipt_enc)
            params["Receipt"] = receipt_enc
        parts.append(self.password1)
        parts.extend(_shp_part(shp))
        params["SignatureValue"] = self.sign(*parts)
        params.update({k: str(v) for k, v in shp.items()})
        if self.is_test:
            params["IsTest"] = "1"
        return f"{PAY_URL}?{urlencode(params)}"

    # ---------- уведомление ResultURL ----------
    def check_result_signature(self, data: dict) -> bool:
        """Подпись уведомления: OutSum:InvId:Пароль#2[:Shp_...]"""
        out_sum, inv_id, signature = data.get("OutSum", ""), data.get("InvId", ""), data.get("SignatureValue", "")
        if not (out_sum and inv_id and signature):
            return False
        shp = {k: v for k, v in data.items() if k.startswith("Shp_")}
        expected = self.sign(out_sum, inv_id, self.password2, *_shp_part(shp))
        return hmac.compare_digest(expected.lower(), signature.strip().lower())

    # ---------- запрос статуса (запасной способ) ----------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def op_state(self, inv_id: int | str) -> dict:
        """Возвращает {'result': код ответа, 'state': код состояния или None, 'out_sum': сумма или None}.
        Подпись запроса: MerchantLogin:InvoiceID:Пароль#2."""
        params = {"MerchantLogin": self.login, "InvoiceID": str(inv_id),
                  "Signature": self.sign(self.login, str(inv_id), self.password2)}
        session = await self._get_session()
        async with session.get(STATE_URL, params=params) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RobokassaError(f"Робокасса ответила {resp.status}: {text[:300]}")
        return parse_op_state(text)


def _find(root: ET.Element, path: list[str]) -> str | None:
    """Поиск по цепочке тегов без учёта XML-пространства имён."""
    node = root
    for tag in path:
        node = next((c for c in node if c.tag.split("}")[-1] == tag), None)
        if node is None:
            return None
    return (node.text or "").strip()


def parse_op_state(xml_text: str) -> dict:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RobokassaError(f"Непонятный ответ Робокассы: {xml_text[:200]}") from e
    result = _find(root, ["Result", "Code"])
    state = _find(root, ["State", "Code"])
    out_sum = _find(root, ["Info", "OutSum"])
    return {
        "result": int(result) if result and result.lstrip("-").isdigit() else None,
        "state": int(state) if state and state.isdigit() else None,
        "out_sum": out_sum,
    }
