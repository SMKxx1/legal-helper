"""Canonical NDA token reference — the content behind the admin Templates *Bulk upload* box's
downloadable PDF (``GET /admin/templates/token-reference.pdf``).

The content lives here as DATA (a scope legend + 4 sections, 16 tokens). The PDF is rendered on the fly
by :func:`build_token_reference_pdf` with reportlab (lazy-imported, so importing this module stays cheap
and the dependency is only touched when someone downloads the reference).
"""

from __future__ import annotations

TITLE = "NDA Template — Token Reference"
INTRO = (
    "Final canonical token reference for all 8 NDA templates, with expected values "
    "and scope per token."
)

#: Scope code → what it means (rendered as the legend row).
SCOPE_LEGEND: list[tuple[str, str]] = [
    ("all docs", "Present in all 8"),
    ("company / SP", "Companies & service providers"),
    ("individual", "Individual templates"),
    ("SP only", "Service-provider templates"),
    ("mNDA only", "Mutual NDA templates"),
]

#: (section title, [(token, expected value, scope), …]) — the 16 canonical tokens grouped.
SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Amperesand / internal",
        [
            (
                "{{amperesand_signer_name}}",
                "Full name of Amperesand's authorized signatory",
                "all docs",
            ),
            (
                "{{amperesand_signer_title}}",
                'Job title of Amperesand\'s signatory (e.g. "CEO")',
                "all docs",
            ),
        ],
    ),
    (
        "Counterparty — identity",
        [
            (
                "{{counterparty_name}}",
                "Full legal name of the counterparty (person or entity)",
                "all docs",
            ),
            (
                "{{counterparty_signer_name}}",
                "Name of the authorized representative signing on behalf of the entity",
                "company / SP",
            ),
            (
                "{{counterparty_signer_title}}",
                "Job title of the counterparty's signatory",
                "company / SP",
            ),
            (
                "{{individual_id_number}}",
                "NRIC, passport number, or equivalent government-issued personal ID",
                "individual",
            ),
            (
                "{{counterparty_company_registration_number}}",
                "Official company incorporation / registration number",
                "company / SP",
            ),
            (
                "{{jurisdiction}}",
                'Place of incorporation or governing law jurisdiction (e.g. "Delaware", "Singapore")',
                "company / SP",
            ),
        ],
    ),
    (
        "Counterparty — address & contact",
        [
            (
                "{{street_address}}",
                "Street name, building number, and unit/floor",
                "all docs",
            ),
            ("{{city_zip}}", "City and ZIP / postal code", "all docs"),
            ("{{country}}", "Country name", "all docs"),
            (
                "{{attn}}",
                "Name or title of the contact person for legal notices",
                "company / SP",
            ),
            (
                "{{notice_email}}",
                "Email address for delivery of contractual notices",
                "all docs",
            ),
        ],
    ),
    (
        "Agreement terms",
        [
            (
                "{{effective_date}}",
                'Date the NDA comes into force (e.g. "June 23, 2026")',
                "all docs",
            ),
            (
                "{{purpose}}",
                "Plain-language description of what the NDA covers "
                '(e.g. "evaluation of a potential business partnership")',
                "mNDA only",
            ),
            (
                "{{services}}",
                "Description of services the service provider will render under the agreement",
                "SP only",
            ),
        ],
    ),
]

PDF_FILENAME = "NDA-Token-Reference.pdf"


def build_token_reference_pdf() -> bytes:
    """Render the token reference to a clean, branded A4 PDF and return the bytes.

    A newly designed layout (NOT a copy of any source HTML): a navy title bar, the scope legend, then
    one gold-headed table per section with columns Token / Expected value / Scope. reportlab is
    lazy-imported so this module imports cheaply and the dependency is only exercised on download.
    """
    from io import BytesIO
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    navy = colors.HexColor("#0C1218")
    gold = colors.HexColor("#B7965D")
    rule = colors.HexColor("#E3DCCB")
    ink = colors.HexColor("#1C2530")
    muted = colors.HexColor("#5B6672")
    zebra = colors.HexColor("#FBF8F1")

    title_style = ParagraphStyle(
        "amp_title", fontName="Helvetica-Bold", fontSize=20, textColor=navy, leading=24
    )
    intro_style = ParagraphStyle(
        "amp_intro", fontName="Helvetica", fontSize=10, textColor=muted, leading=14
    )
    legend_style = ParagraphStyle(
        "amp_legend", fontName="Helvetica", fontSize=8.5, textColor=ink, leading=13
    )
    section_style = ParagraphStyle(
        "amp_section",
        fontName="Helvetica-Bold",
        fontSize=12,
        textColor=gold,
        leading=16,
        spaceBefore=4,
        spaceAfter=4,
    )
    head_style = ParagraphStyle(
        "amp_head", fontName="Helvetica-Bold", fontSize=9, textColor=colors.white
    )
    code_style = ParagraphStyle(
        "amp_code", fontName="Courier-Bold", fontSize=8.5, textColor=navy, leading=12
    )
    body_style = ParagraphStyle(
        "amp_body", fontName="Helvetica", fontSize=9, textColor=ink, leading=12
    )
    scope_style = ParagraphStyle(
        "amp_scope",
        fontName="Helvetica-Oblique",
        fontSize=8.5,
        textColor=muted,
        leading=12,
    )

    story: list = [
        Paragraph(escape(TITLE), title_style),
        Spacer(1, 2 * mm),
        Paragraph(escape(INTRO), intro_style),
        Spacer(1, 5 * mm),
        Paragraph(
            "  ·  ".join(
                f"<b>{escape(code)}</b> — {escape(mean)}" for code, mean in SCOPE_LEGEND
            ),
            legend_style,
        ),
        Spacer(1, 6 * mm),
    ]

    col_widths = [58 * mm, 92 * mm, 28 * mm]
    for section_title, rows in SECTIONS:
        story.append(Paragraph(escape(section_title), section_style))
        data = [
            [
                Paragraph("Token", head_style),
                Paragraph("Expected value", head_style),
                Paragraph("Scope", head_style),
            ]
        ]
        for token, expected, scope in rows:
            data.append(
                [
                    Paragraph(escape(token), code_style),
                    Paragraph(escape(expected), body_style),
                    Paragraph(escape(scope), scope_style),
                ]
            )
        table = Table(data, colWidths=col_widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), navy),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, rule),
            ("LINEBELOW", (0, 0), (-1, 0), 0, navy),
        ]
        for r in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, r), (-1, r), zebra))
        table.setStyle(TableStyle(style))
        story.append(table)
        story.append(Spacer(1, 6 * mm))

    buf = BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=TITLE,
        topMargin=18 * mm,
        bottomMargin=16 * mm,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
    ).build(story)
    return buf.getvalue()
