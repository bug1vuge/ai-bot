"""Персональный водяной знак: на каждой странице PDF внизу печатается имя и ID покупателя.
Это не мешает чтению, но отбивает желание выкладывать файл в открытый доступ."""
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_FONT_NAME = "WatermarkFont"
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    str(Path(__file__).resolve().parent.parent / "data" / "DejaVuSans.ttf"),
    "C:/Windows/Fonts/arial.ttf",                       # Windows
    "/System/Library/Fonts/Supplemental/Arial.ttf",    # macOS
    "/Library/Fonts/Arial.ttf",
]
_font_ready = False


def _ensure_font() -> str:
    global _font_ready
    if _font_ready:
        return _FONT_NAME
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            pdfmetrics.registerFont(TTFont(_FONT_NAME, path))
            _font_ready = True
            return _FONT_NAME
    raise RuntimeError("Не найден шрифт DejaVuSans.ttf для водяного знака (нужна кириллица).")


def _overlay(width: float, height: float, text: str) -> PdfReader:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setFont(_ensure_font(), 6.5)
    c.setFillColorRGB(0.55, 0.55, 0.62, alpha=0.75)
    c.drawCentredString(width / 2, 14, text)
    c.save()
    buf.seek(0)
    return PdfReader(buf)


def stamp_pdf(source: Path, text: str) -> bytes:
    # clone_from сохраняет закладки, внутренние ссылки оглавления и метаданные
    writer = PdfWriter(clone_from=str(source))
    cache: dict[tuple[int, int], object] = {}
    for page in writer.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        key = (round(w), round(h))
        if key not in cache:
            cache[key] = _overlay(w, h, text).pages[0]
        page.merge_page(cache[key])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
