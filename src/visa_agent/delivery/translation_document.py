"""Render an English translation draft beside the source text for review."""

from __future__ import annotations

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from visa_agent.delivery.itinerary_document import _chinese_font


def translation_pdf(filename: str, source_hash: str, pages: list[list[tuple[str, str]]]) -> bytes:
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, title="English translation draft")
    title = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=19, leading=25)
    plain = ParagraphStyle("Body", fontName=_chinese_font(), fontSize=10, leading=15, wordWrap="CJK")
    english = ParagraphStyle("English", fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=8)
    source = ParagraphStyle("Source", parent=plain, textColor=colors.HexColor("#516271"), fontSize=9, leading=13)
    story = []
    for index, page in enumerate(pages, 1):
        if index > 1:
            story.append(PageBreak())
        story.extend([
            Paragraph("English translation draft", title),
            Paragraph(escape(f"Source: {filename} · {source_hash[:12]} · original page {index}/{len(pages)}"), plain),
            Paragraph("Review each line against the original document.", english),
            Spacer(1, 8),
        ])
        for original, translation in page:
            story.append(KeepTogether([
                Paragraph(escape(original), source),
                Paragraph(escape(translation), english),
            ]))
    doc.build(story)
    return output.getvalue()
