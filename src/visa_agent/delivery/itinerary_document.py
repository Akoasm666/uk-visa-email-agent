"""Render an itinerary with a daily schedule, transport and overnight stops."""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from visa_agent.ai_capabilities.visitor_itinerary import needs_english_translation

PDF_MIME = "application/pdf"
_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2}(?: — \d{4}-\d{2}-\d{2})?) {2}(.+)$")


@lru_cache(maxsize=1)
def _chinese_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont, TTFError

    for candidate in (
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    ):
        if candidate.is_file():
            try:
                pdfmetrics.registerFont(TTFont("ItineraryChinese", str(candidate), subfontIndex=0))
                return "ItineraryChinese"
            except TTFError:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def itinerary_pdf(draft: str, *, version: int) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    if needs_english_translation(draft):
        raise ValueError("Itinerary PDF content must be in English")
    font = "Helvetica"
    heading_font = "Helvetica-Bold"
    ink = colors.HexColor("#182738")
    secondary = colors.HexColor("#34475B")
    pale = colors.HexColor("#F4F7FB")
    border = colors.HexColor("#D9E1E9")
    title = "United Kingdom Travel Itinerary"
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream, pagesize=A4, title=title, author="Visa Agent", subject=f"Itinerary revision {version}",
        leftMargin=38, rightMargin=38, topMargin=42, bottomMargin=44,
    )

    base = ParagraphStyle("Body", fontName=font, fontSize=9.5, leading=14, textColor=ink)
    heading = ParagraphStyle("Title", parent=base, fontName=heading_font, fontSize=23, leading=29)
    meta = ParagraphStyle("Meta", parent=base, fontSize=10, leading=17, textColor=secondary)
    date_style = ParagraphStyle("Date", parent=base, fontSize=9, alignment=TA_CENTER)
    header_style = ParagraphStyle("Header", parent=base, fontName=heading_font,
                                  textColor=colors.white)
    day_heading = ParagraphStyle("Day", parent=base, fontName=heading_font,
                                 textColor=colors.HexColor("#164C73"), spaceAfter=3)
    rows = [match.groups() for line in draft.splitlines() if (match := _ROW.match(line))]
    if not rows:
        raise ValueError("Itinerary must contain at least one dated row")
    first, last = rows[0][0].split(" — ")[0], rows[-1][0].split(" — ")[-1]
    nights = (date.fromisoformat(last) - date.fromisoformat(first)).days
    summary = draft.splitlines()[1] if len(draft.splitlines()) > 1 and not _ROW.match(draft.splitlines()[1]) else ""
    story = [
        Paragraph(title, heading),
        Spacer(1, 5),
        Paragraph(f"{first} to {last}  ·  {nights + 1} days / {nights} nights", meta),
        Spacer(1, 7),
        Paragraph(escape(summary or "All times are suggested local times."), meta),
        Spacer(1, 13),
    ]
    columns = ("Day and date", "Area and activities", "Transport", "Overnight city")
    table_rows = [[Paragraph(label, header_style) for label in columns]]
    day_number = 1
    for dates, entry in rows:
        date_span = dates.replace(" — ", "<br/>-<br/>")
        if " — " in dates:
            a, _, b = dates.partition(" — ")
            count = (date.fromisoformat(b) - date.fromisoformat(a)).days + 1
            display_day = f"{day_number}-{day_number + count - 1}"
        else:
            count = 1
            display_day = str(day_number)
        day_number += count
        date_cell = Paragraph(f"Day {display_day}<br/>{date_span}", date_style)
        segments = entry.split(" | ")
        if len(segments) >= 4:
            activities = [Paragraph(escape(segments[0]), day_heading)]
            activities.extend(Paragraph(escape(activity), base) for activity in segments[1:-2])
            travel, overnight = segments[-2:]
        else:
            activities = [Paragraph(escape(entry), base)]
            travel = overnight = "—"
        table_rows.append([
            date_cell, activities, Paragraph(escape(travel), base), Paragraph(escape(overnight), base),
        ])

    table = Table(table_rows, colWidths=[doc.width * part for part in (0.158, 0.497, 0.209, 0.136)],
                  repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#254564")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, pale]),
        ("GRID", (0, 0), (-1, -1), 0.45, border),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    def page_number(canvas, page) -> None:
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(secondary)
        canvas.drawRightString(A4[0] - page.rightMargin, 23, str(page.page))

    doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return stream.getvalue()
