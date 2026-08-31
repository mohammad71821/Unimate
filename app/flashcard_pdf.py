import io

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

pdfmetrics.registerFont(TTFont("Vazir", "app/fonts/Vazirmatn-Regular.ttf"))
pdfmetrics.registerFont(TTFont("Vazir-Bold", "app/fonts/Vazirmatn-Bold.ttf"))

# رنگ‌هایی که بین کارت‌ها می‌چرخن: آبی، سبزآبی، سبز (پس‌زمینه‌ی روشن + متن تیره‌ی هم‌خانواده)
CARD_COLORS = [
    (colors.HexColor("#E6F1FB"), colors.HexColor("#042C53")),
    (colors.HexColor("#E1F5EE"), colors.HexColor("#04342C")),
    (colors.HexColor("#EAF3DE"), colors.HexColor("#173404")),
]

CARDS_PER_PAGE = 10


def _fa(text: str) -> str:
    """متن فارسی رو برای نمایش درست (اتصال حروف + راست‌به‌چپ) توی PDF آماده می‌کنه."""
    if not text:
        return ""
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def build_flashcard_pdf_bytes(cards: list[dict], title: str) -> bytes:
    if not cards:
        raise ValueError("No flashcards to render")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
    )

    title_style = ParagraphStyle(
        "title", fontName="Vazir-Bold", fontSize=16, alignment=TA_RIGHT, spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "meta", fontName="Vazir", fontSize=9, alignment=TA_RIGHT,
        textColor=colors.HexColor("#5F5E5A"), spaceAfter=14,
    )

    story = [
        Paragraph(_fa(title), title_style),
        Paragraph(_fa(f"{len(cards)} فلش‌کارت"), meta_style),
    ]

    for i, card in enumerate(cards):
        bg, fg = CARD_COLORS[i % len(CARD_COLORS)]

        q_style = ParagraphStyle(
            "q", fontName="Vazir-Bold", fontSize=11.5, alignment=TA_RIGHT, textColor=fg, leading=16,
        )
        a_style = ParagraphStyle(
            "a", fontName="Vazir", fontSize=10, alignment=TA_RIGHT, textColor=fg, leading=14, spaceBefore=3,
        )

        q_text = _fa(f"{card['question']}  .{i + 1}")
        cell = [Paragraph(q_text, q_style), Paragraph(_fa(card["answer"]), a_style)]

        table = Table([[cell]], colWidths=[doc.width])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ]))

        story.append(table)
        story.append(Spacer(1, 6))

        if (i + 1) % CARDS_PER_PAGE == 0 and (i + 1) < len(cards):
            story.append(PageBreak())

    doc.build(story)
    return buffer.getvalue()
