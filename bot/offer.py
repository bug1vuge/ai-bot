"""Страница публичной оферты /offer. Текст берётся из data/offer.md (редактируйте его),
а ФИО, ИНН, цена, ник бота и контакты подставляются из .env автоматически."""
import html
import re
from datetime import datetime, timezone

from .config import BASE_DIR, Config

OFFER_FILE = BASE_DIR / "data" / "offer.md"
MISSING = "[НЕ ЗАПОЛНЕНО]"


def offer_values(cfg: Config, bot_username: str) -> dict:
    mtime = OFFER_FILE.stat().st_mtime if OFFER_FILE.exists() else datetime.now(timezone.utc).timestamp()
    return {
        "seller_name": cfg.seller_name or MISSING,
        "seller_inn": cfg.seller_inn or MISSING,
        "seller_email": cfg.seller_email or MISSING,
        "price": f"{cfg.price_rub:,}".replace(",", " "),
        "bot": f"@{bot_username}" if bot_username else MISSING,
        "support": f"@{cfg.support_username}" if cfg.support_username else MISSING,
        "date": datetime.fromtimestamp(mtime, timezone.utc).strftime("%d.%m.%Y"),
    }


def missing_fields(cfg: Config) -> list[str]:
    names = {"SELLER_NAME": cfg.seller_name, "SELLER_INN": cfg.seller_inn,
             "SELLER_EMAIL": cfg.seller_email, "SUPPORT_USERNAME": cfg.support_username}
    return [k for k, v in names.items() if not v]


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)
    return text


def markdown_to_html(md: str) -> str:
    """Простой разбор: заголовки #, ##, абзацы; строки внутри абзаца — через перенос."""
    out, para = [], []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    for line in md.splitlines():
        s = line.strip()
        if not s:
            flush()
        elif s.startswith("## "):
            flush()
            out.append(f"<h2>{_inline(s[3:])}</h2>")
        elif s.startswith("# "):
            flush()
            out.append(f"<h1>{_inline(s[2:])}</h1>")
        else:
            para.append(s)
    flush()
    return "\n".join(out)


def render_offer(cfg: Config, bot_username: str) -> str:
    md = OFFER_FILE.read_text(encoding="utf-8") if OFFER_FILE.exists() else "# Оферта не найдена"
    values = offer_values(cfg, bot_username)
    md = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), md)
    body = markdown_to_html(md)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Публичная оферта — ИИ без тёмного леса</title>
<style>
  :root {{ --ink:#17162B; --muted:#5E5B78; --accent:#5B4BFF; --bg:#F7F6FC; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.6 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }}
  main {{ max-width:760px; margin:0 auto; padding:32px 16px 64px; background:#fff; min-height:100vh; box-sizing:border-box; }}
  h1 {{ font-size:26px; line-height:1.25; margin:0 0 8px; }}
  h2 {{ font-size:19px; margin:28px 0 8px; color:var(--accent); }}
  p {{ margin:0 0 12px; }}
  @media (min-width:800px) {{ main {{ padding:48px 56px 72px; margin-top:24px; border-radius:16px; min-height:auto; }} }}
</style></head>
<body><main>
{body}
</main></body></html>"""
