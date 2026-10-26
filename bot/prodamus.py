"""Работа с Prodamus: ссылка на оплату и проверка подписи уведомлений об оплате.

Ссылка на оплату — платёжная страница магазина (https://<адрес>.payform.ru) с параметрами заказа
в строке запроса: do=pay сразу открывает оплату. Сумма в ссылке ничего не гарантирует —
покупатель может её изменить, поэтому бот сверяет сумму из подписанного уведомления с заказом.

Уведомление об оплате приходит POST-запросом на адрес из настроек платёжной страницы.
Подпись — заголовок Sign: HMAC-SHA256 секретным ключом от JSON-представления данных запроса,
в котором все значения приведены к строкам, а ключи рекурсивно отсортированы
(как в PHP-классе Hmac из документации Prodamus).
"""
import hashlib
import hmac
import json
import logging
import re
from urllib.parse import parse_qsl, urlencode

log = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
_KEY_RE = re.compile(r"^([^\[\]]+)((?:\[[^\[\]]*\])*)$")


class ProdamusError(Exception):
    pass


# ---------- разбор тела запроса в структуру как у PHP ($_POST) ----------
def parse_form(body: str) -> dict:
    """«products[0][name]=x&sum=1» → {"products": [{"name": "x"}], "sum": "1"}."""
    root: dict = {}
    for key, value in parse_qsl(body, keep_blank_values=True):
        m = _KEY_RE.match(key)
        if not m:
            root[key] = value
            continue
        path = [m.group(1)] + re.findall(r"\[([^\[\]]*)\]", m.group(2))
        node = root
        for i, part in enumerate(path):
            last = i == len(path) - 1
            if part == "":  # «a[]» — следующий индекс
                part = str(len(node))
            if last:
                node[part] = value
            else:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = node[part] = {}
                node = nxt
    return _lists(root)


def _lists(value):
    """Словари с ключами 0..n-1 становятся списками — так их кодирует json_encode в PHP."""
    if isinstance(value, dict):
        value = {k: _lists(v) for k, v in value.items()}
        keys = list(value)
        if keys and all(k.isdigit() for k in keys) and sorted(int(k) for k in keys) == list(range(len(keys))):
            return [value[str(i)] for i in range(len(keys))]
        return value
    if isinstance(value, list):
        return [_lists(v) for v in value]
    return value


def _normalize(value):
    """Как в PHP-классе Hmac: все значения — строки, ключи словарей отсортированы рекурсивно."""
    if isinstance(value, dict):
        return {str(k): _normalize(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value)


def _json_variants(data) -> list[str]:
    """JSON так, как его строит PHP json_encode(..., JSON_UNESCAPED_UNICODE).
    PHP по умолчанию экранирует «/» как «\\/», другие реализации — нет; принимаем оба варианта
    (подпись всё равно невозможно подобрать без секретного ключа)."""
    plain = json.dumps(_normalize(data), ensure_ascii=False, separators=(",", ":"))
    variants = [plain, plain.replace("/", "\\/")]
    if "\u2028" in plain or "\u2029" in plain:
        variants += [v.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") for v in variants]
    return variants


def sign(data, secret: str) -> str:
    """Подпись в формате Prodamus (вариант PHP, со слэшами «\\/»)."""
    return hmac.new(secret.encode(), _json_variants(data)[1].encode(), hashlib.sha256).hexdigest()


def verify(data, secret: str, signature: str) -> bool:
    if not (secret and signature):
        return False
    signature = signature.strip().lower()
    return any(
        hmac.compare_digest(hmac.new(secret.encode(), v.encode(), hashlib.sha256).hexdigest(), signature)
        for v in _json_variants(data)
    )


class Prodamus:
    def __init__(self, form_url: str, secret: str):
        self.form_url = form_url.rstrip("/")
        self.secret = secret

    def payment_url(self, order_id: str, amount: str, product_name: str, *, success_url: str = "",
                    return_url: str = "", customer_extra: str = "") -> str:
        """Ссылка на оплату одного товара. do=pay — покупатель сразу попадает на оплату."""
        params: dict[str, str] = {
            "do": "pay",
            "order_id": order_id,
            "products[0][name]": product_name[:128],
            "products[0][price]": amount,
            "products[0][quantity]": "1",
            "products[0][sku]": "guide-pdf",
            # самозанятый: чек в «Мой налог» формируется на доход от физического лица
            "npd_income_type": "FROM_INDIVIDUAL",
        }
        if customer_extra:
            params["customer_extra"] = customer_extra[:200]
        if success_url:
            params["urlSuccess"] = success_url
        if return_url:
            params["urlReturn"] = return_url
        return f"{self.form_url}/?{urlencode(params)}"

    def parse_notification(self, body: bytes, content_type: str, signature: str) -> dict:
        """Проверяет подпись уведомления и возвращает его данные. Ошибка — ProdamusError."""
        text = body.decode("utf-8", errors="strict")
        if "json" in (content_type or ""):
            try:
                data = json.loads(text)
            except ValueError as e:
                raise ProdamusError("тело уведомления — не JSON") from e
            if not isinstance(data, dict):
                raise ProdamusError("тело уведомления — не объект")
        else:
            data = parse_form(text)
        if not data:
            raise ProdamusError("пустое уведомление")
        if not signature:
            raise ProdamusError("нет заголовка Sign")
        candidates = [data]
        if isinstance(data.get("submit"), dict):
            candidates.append(data["submit"])  # режим callbackType=json подписывает вложенный объект
        if not any(verify(c, self.secret, signature) for c in candidates):
            raise ProdamusError("неверная подпись")
        return data


def notification_fields(data: dict) -> dict:
    """Нужные боту поля уведомления: номер заказа бота, сумма, статус, номер заказа в Prodamus."""
    src = data.get("submit") if isinstance(data.get("submit"), dict) and "order_num" not in data else data
    return {
        "order": str(src.get("order_num") or "").strip(),
        "prodamus_order": str(src.get("order_id") or "").strip(),
        "sum": str(src.get("sum") or "").strip(),
        "status": str(src.get("payment_status") or "").strip().lower(),
        "status_text": str(src.get("payment_status_description") or "").strip(),
    }
