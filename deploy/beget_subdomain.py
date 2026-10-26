"""Создаёт поддомен в DNS Beget и направляет его на этот сервер.

Запуск на сервере:   python3 deploy/beget_subdomain.py aibezlesa.floxy.pro
                     python3 deploy/beget_subdomain.py aibezlesa.floxy.pro 159.194.199.105   (IP вручную)

Нужны логин Beget и пароль API (Панель Beget → Настройки → Ограничения доступа → пароль для API;
если отдельный пароль не задан, подходит обычный пароль аккаунта). Логин и пароль нигде не сохраняются.
"""
import getpass
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.beget.com/api/"


class BegetError(Exception):
    pass


def call(method: str, login: str, password: str, data: dict) -> object:
    body = urllib.parse.urlencode({
        "login": login, "passwd": password,
        "input_format": "json", "output_format": "json",
        "input_data": json.dumps(data, ensure_ascii=False),
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(API + method, data=body), timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("status") != "success":
        raise BegetError(f"Beget отклонил запрос: {resp.get('error_text') or resp.get('errors') or resp}")
    answer = resp.get("answer", {})
    if isinstance(answer, dict) and answer.get("status") == "error":
        raise BegetError(f"Ошибка Beget ({method}): {answer.get('errors') or answer}")
    return answer.get("result") if isinstance(answer, dict) else answer


def set_a_record(login, password, fqdn, ip):
    return call("dns/changeRecords", login, password,
                {"fqdn": fqdn, "records": {"A": [{"priority": 10, "value": ip}]}})


def create_subdomain(login, password, fqdn):
    label, parent = fqdn.split(".", 1)
    domains = call("domain/getList", login, password, {}) or []
    match = next((d for d in domains if str(d.get("fqdn", "")).lower() == parent.lower()), None)
    if match is None:
        names = ", ".join(str(d.get("fqdn")) for d in domains) or "нет доменов"
        raise BegetError(f"Домен {parent} не найден в этом аккаунте Beget. Есть: {names}")
    return call("domain/addSubdomainVirtual", login, password, {"subdomain": label, "domain_id": match["id"]})


def public_ip() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
        return r.read().decode().strip()


def resolve_at_beget(fqdn: str) -> str:
    if not shutil.which("dig"):
        return ""
    out = subprocess.run(["dig", "+short", "A", fqdn, "@ns1.beget.com"], capture_output=True, text=True)
    return out.stdout.strip()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1].count(".") < 2:
        sys.exit("Использование: python3 deploy/beget_subdomain.py поддомен.домен.зона [IP]")
    fqdn = sys.argv[1].strip().lower().rstrip(".")
    ip = sys.argv[2].strip() if len(sys.argv) > 2 else public_ip()
    print(f"Создаю запись: {fqdn} → {ip}")
    login = input("Логин Beget: ").strip()
    password = getpass.getpass("Пароль API Beget (символы не отображаются): ")

    try:
        try:
            set_a_record(login, password, fqdn, ip)
        except BegetError as first:
            if "отклонил" in str(first):
                raise  # неверный логин/пароль или API выключен — создавать поддомен бессмысленно
            print("Поддомена ещё нет в Beget — создаю его…")
            create_subdomain(login, password, fqdn)
            time.sleep(3)
            set_a_record(login, password, fqdn, ip)
    except BegetError as e:
        sys.exit(f"\n[x] {e}\n    Проверьте логин и пароль API (Панель Beget → Настройки → Ограничения доступа)"
                 f" или создайте поддомен в панели Beget вручную.")
    except OSError as e:
        sys.exit(f"\n[x] Не удалось связаться с API Beget: {e}")

    print(f"\n✅ Запись создана: {fqdn} → {ip}")
    for _ in range(12):
        got = resolve_at_beget(fqdn)
        if ip in got.split():
            print(f"✅ DNS Beget уже отвечает: {fqdn} → {ip}")
            break
        time.sleep(10)
    else:
        print("DNS Beget ещё обновляется — это нормально, обычно занимает 5–30 минут.")
    print(f"\nДальше: дождитесь, пока команда  getent hosts {fqdn}  покажет {ip},")
    print(f"и запустите:  bash deploy/install.sh {fqdn}")


if __name__ == "__main__":
    main()
