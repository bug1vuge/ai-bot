"""Безопасно записывает значение в .env: python3 set_env.py путь/к/.env КЛЮЧ ЗНАЧЕНИЕ
Работает с любыми символами в значении (пароли с $, /, & и т.п.)."""
import sys
from pathlib import Path


def _quote(value: str) -> str:
    """Значения с пробелами, #, кавычками или обратной косой чертой берём в двойные кавычки."""
    if value and not any(c in value for c in " #'\"\\\t"):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def set_env(path: Path, key: str, value: str) -> None:
    value = _quote(value)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def get_env(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#") and "=" in line:
            return _unquote(line.split("=", 1)[1])
    return ""


if __name__ == "__main__":
    if len(sys.argv) == 3:
        print(get_env(Path(sys.argv[1]), sys.argv[2]))
    elif len(sys.argv) == 4:
        set_env(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
    else:
        sys.exit("Использование: set_env.py .env КЛЮЧ [ЗНАЧЕНИЕ]")
