"""Общие фикстуры. Настоящий гайд в репозиторий не входит, поэтому тесты собирают
небольшой PDF с закладками сами — так они работают в любой копии проекта и в CI."""
import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

PAGES = 6


def build_sample_pdf(path: Path) -> Path:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    for i in range(PAGES):
        c.drawString(72, 770, f"Sample page {i + 1}")
        c.showPage()
    c.save()
    buf.seek(0)
    writer = PdfWriter(clone_from=buf)
    for i in range(0, PAGES, 2):
        writer.add_outline_item(f"Chapter {i // 2 + 1}", i)
    with open(path, "wb") as f:
        writer.write(f)
    return path


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory) -> Path:
    return build_sample_pdf(tmp_path_factory.mktemp("product") / "guide.pdf")
