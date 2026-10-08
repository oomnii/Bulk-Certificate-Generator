"""Single predefined certificate layout.

The bundled DejaVu Sans font is redistributable and covers Latin, Greek, and
Cyrillic. CJK glyphs are outside that font and are a documented limitation.
User text is drawn with the canvas API, not ReportLab's XML paragraphs, so
characters such as ``<`` cannot change the layout.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)

NAVY = HexColor("#1B3A4B")
GOLD = HexColor("#B8952C")
CREAM = HexColor("#FBF7EF")
INK = HexColor("#243038")
MUTED = HexColor("#5E6A71")

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_register_lock = threading.Lock()
_fonts_registered = False


def register_fonts() -> None:
    global _fonts_registered
    with _register_lock:
        if _fonts_registered:
            return
        regular = FONT_DIR / "DejaVuSans.ttf"
        bold = FONT_DIR / "DejaVuSans-Bold.ttf"
        if not regular.is_file() or not bold.is_file():
            raise FileNotFoundError(
                "DejaVu font files are missing. Expected assets/fonts/DejaVuSans.ttf "
                "and assets/fonts/DejaVuSans-Bold.ttf"
            )
        pdfmetrics.registerFont(TTFont("DejaVu", str(regular)))
        pdfmetrics.registerFont(TTFont("DejaVuBold", str(bold)))
        _fonts_registered = True


def format_event_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def render_certificate_pdf(
    destination: Path,
    *,
    recipient_name: str,
    course_name: str,
    event_date: date,
    issued_by: str,
    certificate_id: str,
) -> None:
    register_fonts()
    pdf = canvas.Canvas(str(destination), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.setTitle("Certificate of Completion")
    pdf.setAuthor(issued_by)
    _draw_frame(pdf)
    _draw_body(
        pdf,
        recipient_name=recipient_name,
        course_name=course_name,
        event_date=event_date,
        issued_by=issued_by,
        certificate_id=certificate_id,
    )
    pdf.showPage()
    pdf.save()


def _draw_frame(pdf: canvas.Canvas) -> None:
    pdf.setFillColor(CREAM)
    pdf.rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, fill=1, stroke=0)
    pdf.setStrokeColor(NAVY)
    pdf.setLineWidth(2.5)
    pdf.rect(18, 18, PAGE_WIDTH - 36, PAGE_HEIGHT - 36, fill=0, stroke=1)
    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1)
    pdf.rect(26, 26, PAGE_WIDTH - 52, PAGE_HEIGHT - 52, fill=0, stroke=1)


def _draw_body(
    pdf: canvas.Canvas,
    *,
    recipient_name: str,
    course_name: str,
    event_date: date,
    issued_by: str,
    certificate_id: str,
) -> None:
    center = PAGE_WIDTH / 2
    content_width = PAGE_WIDTH - 150
    y = PAGE_HEIGHT - 92

    pdf.setFillColor(GOLD)
    pdf.setFont("DejaVu", 12)
    pdf.drawCentredString(center, y, "CERTIFICATE OF COMPLETION")
    y -= 18
    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1.25)
    pdf.line(center - 170, y, center + 170, y)

    y -= 42
    pdf.setFillColor(MUTED)
    pdf.setFont("DejaVu", 12)
    pdf.drawCentredString(center, y, "This is to certify that")

    name_lines, name_size = _fit_lines(recipient_name, "DejaVuBold", 28, 12, content_width, 4)
    y -= 16
    for line in name_lines:
        y -= name_size + 6
        pdf.setFillColor(NAVY)
        pdf.setFont("DejaVuBold", name_size)
        pdf.drawCentredString(center, y, line)

    y -= 28
    pdf.setFillColor(MUTED)
    pdf.setFont("DejaVu", 12)
    pdf.drawCentredString(center, y, "has successfully completed")

    course_lines, course_size = _fit_lines(course_name, "DejaVuBold", 18, 11, content_width, 3)
    y -= 12
    for line in course_lines:
        y -= course_size + 5
        pdf.setFillColor(INK)
        pdf.setFont("DejaVuBold", course_size)
        pdf.drawCentredString(center, y, line)

    y -= 32
    pdf.setFillColor(INK)
    pdf.setFont("DejaVu", 12)
    pdf.drawCentredString(center, y, f"Awarded on {format_event_date(event_date)}")

    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(0.8)
    pdf.line(140, 108, PAGE_WIDTH - 140, 108)

    issuer_lines, issuer_size = _fit_lines(issued_by, "DejaVuBold", 12, 9, content_width, 2)
    issuer_y = 78
    for line in issuer_lines:
        pdf.setFillColor(NAVY)
        pdf.setFont("DejaVuBold", issuer_size)
        pdf.drawCentredString(center, issuer_y, line)
        issuer_y -= issuer_size + 3

    pdf.setFillColor(MUTED)
    pdf.setFont("DejaVu", 9)
    pdf.drawCentredString(center, 42, f"Certificate ID {certificate_id}")


def _fit_lines(
    text: str,
    font: str,
    start_size: float,
    min_size: float,
    max_width: float,
    max_lines: int,
) -> tuple[list[str], float]:
    """Shrink until the text fits in ``max_lines``.

    ``min_size`` is only a preference. A 200-character course or issuer can still
    exceed the line budget at that size, which collides with the footer, so the
    search continues down to a 6-point floor.
    """
    floor = min(min_size, 6)
    size = start_size
    while size > floor:
        lines = _wrap_text(text, font, size, max_width)
        if len(lines) <= max_lines:
            return lines, size
        size -= 1
    return _wrap_text(text, font, floor, max_width), floor


def _wrap_text(text: str, font: str, size: float, max_width: float) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        pieces = _break_long_word(word, font, size, max_width) or [""]
        for piece in pieces:
            candidate = piece if not current else f"{current} {piece}"
            if pdfmetrics.stringWidth(candidate, font, size) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = piece
    if current:
        lines.append(current)
    return lines or [""]


def _break_long_word(word: str, font: str, size: float, max_width: float) -> list[str]:
    if word == "":
        return []
    if pdfmetrics.stringWidth(word, font, size) <= max_width:
        return [word]
    chunks: list[str] = []
    buffer = ""
    for character in word:
        candidate = buffer + character
        if buffer and pdfmetrics.stringWidth(candidate, font, size) > max_width:
            chunks.append(buffer)
            buffer = character
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    return chunks
