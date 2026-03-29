"""
Invoice PDF generator using ReportLab.
Single consistent style: Helvetica, no italic, bold for headers only.
Dense layout targeting one page per invoice.
"""
import io
import os
from datetime import date as _date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer,
    Table, TableStyle, KeepTogether,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Palette ───────────────────────────────────────────────────────────────────
C_BLACK  = colors.HexColor("#1a1a1a")
C_ACCENT = colors.HexColor("#4a82a8")
C_RULE   = colors.HexColor("#cccccc")
C_STRIPE = colors.HexColor("#f5f5f5")
C_WHITE  = colors.white

# ── Number formatting ─────────────────────────────────────────────────────────

def sek(val):
    """Format a number as Swedish currency: 1 220,00 kr"""
    if val is None:
        return "–"
    abs_val = abs(float(val))
    int_part, dec_part = f"{abs_val:.2f}".split(".")
    grouped = []
    for i, d in enumerate(reversed(int_part)):
        if i and i % 3 == 0:
            grouped.append("\u00a0")
        grouped.append(d)
    formatted_int = "".join(reversed(grouped))
    sign = "\u2212" if float(val) < 0 else ""
    return f"{sign}{formatted_int},{dec_part}\u00a0kr"


def fmt_hours(h):
    if h == int(h):
        return str(int(h))
    return f"{h:.2f}".replace(".", ",")


# ── Styles ────────────────────────────────────────────────────────────────────

def _styles():
    FONT = "Helvetica"
    FONTB = "Helvetica-Bold"
    s = {}
    s["label"] = ParagraphStyle("label", fontName=FONTB, fontSize=6,
                                 textColor=C_BLACK, leading=8, spaceAfter=1)
    s["value"] = ParagraphStyle("value", fontName=FONT, fontSize=8,
                                 textColor=C_BLACK, leading=10)
    s["value_b"] = ParagraphStyle("value_b", fontName=FONTB, fontSize=8,
                                   textColor=C_BLACK, leading=10)
    s["cell"]  = ParagraphStyle("cell",  fontName=FONT,  fontSize=7.5,
                                 textColor=C_BLACK, leading=9)
    s["cell_b"] = ParagraphStyle("cell_b", fontName=FONTB, fontSize=7.5,
                                  textColor=C_BLACK, leading=9)
    s["small"] = ParagraphStyle("small", fontName=FONT, fontSize=6.5,
                                 textColor=C_BLACK, leading=8)
    s["small_b"] = ParagraphStyle("small_b", fontName=FONTB, fontSize=6.5,
                                   textColor=C_BLACK, leading=8)
    s["footer"] = ParagraphStyle("footer", fontName=FONT, fontSize=6.5,
                                  textColor=C_BLACK, leading=8.5)
    s["footer_b"] = ParagraphStyle("footer_b", fontName=FONTB, fontSize=6.5,
                                    textColor=C_BLACK, leading=8.5)
    s["inv_label"] = ParagraphStyle("inv_label", fontName=FONTB, fontSize=18,
                                     textColor=C_BLACK, leading=22)
    s["company"] = ParagraphStyle("company", fontName=FONTB, fontSize=13,
                                   textColor=C_BLACK, leading=16)
    s["company_sub"] = ParagraphStyle("company_sub", fontName=FONT, fontSize=7,
                                       textColor=C_ACCENT, leading=9)
    return s


# ── Footer callback ───────────────────────────────────────────────────────────

def _make_footer_cb(company, lang):
    def _footer(canvas, doc):
        canvas.saveState()
        W = A4[0]
        y = 12 * mm
        lm = 15 * mm
        rm = 15 * mm
        usable = W - lm - rm
        col = usable / 4

        canvas.setStrokeColor(C_RULE)
        canvas.setLineWidth(0.5)
        canvas.line(lm, y + 9 * mm, W - rm, y + 9 * mm)

        FONT  = "Helvetica"
        FONTB = "Helvetica-Bold"

        def block(x, title, *lines):
            canvas.setFont(FONTB, 6)
            canvas.setFillColor(C_BLACK)
            canvas.drawString(x, y + 7.5 * mm, title)
            cy = y + 5.5 * mm
            for line in lines:
                if line:
                    canvas.setFont(FONT, 6.5)
                    canvas.drawString(x, cy, line)
                    cy -= 3 * mm

        sv = (lang == "sv")
        block(lm,
              "Adress" if sv else "Address",
              company["name"],
              company["address"])
        block(lm + col,
              "Kontakt" if sv else "Contact",
              company.get("phone", ""),
              company.get("email", ""))
        block(lm + 2 * col,
              "Bankgiro",
              company.get("bankgiro", ""),
              "Godkänd för F-skatt" if sv else "Approved for F-tax")
        block(lm + 3 * col,
              "Org.nr / VAT",
              company.get("org_nr", ""),
              company.get("vat_nr", ""))

        canvas.restoreState()
    return _footer


# ── Build context ─────────────────────────────────────────────────────────────

def _ctx(invoice, config):
    lang = invoice.language
    company = {
        "name":      config["COMPANY_NAME"],
        "org_nr":    config["COMPANY_ORG_NR"],
        "address":   config["COMPANY_ADDRESS"],
        "email":     config["COMPANY_EMAIL"],
        "phone":     config["COMPANY_PHONE"],
        "bankgiro":  config["COMPANY_BANKGIRO"],
        "vat_nr":    config["COMPANY_VAT_NR"],
        "reference": config.get("COMPANY_REFERENCE", ""),
    }
    logo_path = os.path.join(os.path.dirname(__file__), config["COMPANY_LOGO_PATH"])
    logo_exists = os.path.exists(logo_path)

    entries = sorted(invoice.time_entries, key=lambda e: e.entry_date)
    total_hours = sum(e.hours for e in entries)

    po_numbers = ", ".join(sorted(set(
        e.hour_type.po.po_number
        for e in entries
        if e.hour_type and e.hour_type.po and e.hour_type.po.po_number
    )))

    rounding = round(invoice.total) - invoice.total

    by_project = {}
    for e in entries:
        pid = e.project_id
        if pid not in by_project:
            by_project[pid] = {"project": e.project, "entries": [], "total": 0.0}
        by_project[pid]["entries"].append(e)
        by_project[pid]["total"] += e.hours

    rate_groups = {}
    for e in entries:
        rate = e.effective_rate
        key = (e.project_id, e.hour_type_id, rate)
        if key not in rate_groups:
            lp = [e.project.name]
            if e.display_type and e.display_type != "Normal":
                lp.append(e.display_type)
            rate_groups[key] = {"label": " – ".join(lp), "hours": 0.0, "rate": rate}
        rate_groups[key]["hours"] += e.hours

    return dict(
        lang=lang, company=company, logo_path=logo_path, logo_exists=logo_exists,
        entries=entries, total_hours=total_hours, po_numbers=po_numbers,
        rounding=rounding, by_project=by_project,
        rate_summary=list(rate_groups.values()),
    )


# ── Invoice PDF ───────────────────────────────────────────────────────────────

def _build_invoice_story(invoice, ctx, s):
    sv   = (ctx["lang"] == "sv")
    co   = ctx["company"]
    story = []

    # ── Header row: logo/name left, FAKTURA right ──
    logo_cell = []
    if ctx["logo_exists"]:
        from reportlab.platypus import Image
        logo_cell = [Image(ctx["logo_path"], width=45*mm, height=14*mm,
                           kind="proportional")]
    else:
        logo_cell = [Paragraph(co["name"], s["company"]),
                     Paragraph("onedesk", s["company_sub"])]

    inv_num_label = "Fakturanr" if sv else "Invoice No."
    inv_date_label = "Fakturadatum" if sv else "Invoice date"
    header_right = [
        Paragraph("FAKTURA" if sv else "INVOICE", s["inv_label"]),
        Spacer(1, 1*mm),
        Table([
            [Paragraph(inv_num_label, s["small_b"]),
             Paragraph(str(invoice.invoice_number), s["small_b"])],
            [Paragraph(inv_date_label, s["small"]),
             Paragraph(str(invoice.issue_date), s["small"])],
        ], colWidths=[28*mm, 28*mm],
           style=TableStyle([
               ("ALIGN", (1,0), (1,-1), "RIGHT"),
               ("TOPPADDING", (0,0), (-1,-1), 1),
               ("BOTTOMPADDING", (0,0), (-1,-1), 1),
           ])),
    ]

    hdr_table = Table(
        [[logo_cell, header_right]],
        colWidths=[90*mm, 90*mm],
        style=TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("ALIGN", (1,0), (1,0), "RIGHT"),
            ("LINEBELOW", (0,0), (-1,0), 1.5, C_BLACK),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]),
    )
    story.append(hdr_table)
    story.append(Spacer(1, 3*mm))

    # ── Customer address ──
    client = invoice.client
    addr_lines = [Paragraph("Fakturaadress" if sv else "Invoice address", s["label"])]
    addr_lines.append(Paragraph(client.name, s["value_b"]))
    if client.address:
        for line in client.address.split("\n"):
            if line.strip():
                addr_lines.append(Paragraph(line.strip(), s["value"]))
    if client.contact_email:
        addr_lines.append(Paragraph(client.contact_email, s["value"]))

    addr_table = Table([[addr_lines, ""]], colWidths=[90*mm, 90*mm],
                       style=TableStyle([("ALIGN", (0,0), (0,0), "LEFT")]))
    story.append(addr_table)
    story.append(Spacer(1, 3*mm))

    # ── Meta grid: 4 columns, 2 per side ──
    def mrow(k, v):
        return [Paragraph(k, s["small"]), Paragraph(str(v) if v else "–", s["small_b"])]

    left_meta = [
        mrow("Kundnr" if sv else "Customer No.",
             client.fortnox_customer_nr or "–"),
        mrow("Er referens" if sv else "Your ref.",
             client.contact_name or "–"),
        mrow("Ert ordernr" if sv else "Your order No.",
             ctx["po_numbers"] or "–"),
        mrow("Ert VAT-nr" if sv else "Your VAT No.",
             client.vat_nr or "–"),
    ]
    right_meta = [
        mrow("Vår referens" if sv else "Our ref.",
             co["reference"] or "–"),
        mrow("Betalningsvillkor" if sv else "Payment terms",
             f"{client.payment_days} {'dagar' if sv else 'days'}"),
        mrow("Förfallodatum" if sv else "Due date",
             str(invoice.due_date)),
        mrow("Leveransdatum" if sv else "Delivery date",
             str(invoice.period_end)),
    ]

    def meta_col_table(rows):
        return Table(rows, colWidths=[26*mm, 37*mm],
                     style=TableStyle([
                         ("TOPPADDING",    (0,0), (-1,-1), 2),
                         ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                         ("LINEBELOW",     (0,0), (-1,-2), 0.4, C_RULE),
                         ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
                     ]))

    meta_table = Table(
        [[meta_col_table(left_meta), meta_col_table(right_meta)]],
        colWidths=[65*mm, 65*mm],
        style=TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (1,0), (1,0), 8),
        ]),
    )
    story.append(meta_table)
    story.append(Spacer(1, 4*mm))

    # ── Line items table ──
    COLS = [62*mm, 13*mm, 11*mm, 22*mm, 10*mm, 18*mm, 22*mm]

    def th(txt):
        return Paragraph(txt, s["cell_b"])

    def td(txt, align="left"):
        return Paragraph(txt, s["cell"])

    def tdr(txt):  # right-align placeholder — handled by TableStyle
        return Paragraph(txt, s["cell"])

    header_row = [
        th("Beskrivning" if sv else "Description"),
        th("Antal" if sv else "Qty"),
        th("Enhet" if sv else "Unit"),
        th("à pris" if sv else "Unit price"),
        th("Moms" if sv else "VAT"),
        th("Moms kr" if sv else "VAT amt"),
        th("Belopp" if sv else "Amount"),
    ]

    rows = [header_row]
    row_colors = []  # (row_index, color)

    for e in ctx["entries"]:
        rate   = e.effective_rate
        amount = e.hours * rate
        vat_kr = amount * 0.25
        desc   = str(e.entry_date)
        if e.description:
            desc += "  " + e.description
        if e.display_type and e.display_type != "Normal":
            desc += "  " + e.display_type
        rows.append([
            td(desc),
            tdr(fmt_hours(e.hours)),
            td("tim" if sv else "hr"),
            tdr(sek(rate)),
            tdr("25%"),
            tdr(sek(vat_kr)),
            tdr(sek(amount)),
        ])

    if invoice.expenses:
        # Section header
        rows.append([
            Paragraph("Utlägg" if sv else "Expenses", s["cell_b"]),
            "", "", "", "", "", "",
        ])
        row_colors.append(len(rows) - 1)  # will style this row

        for exp in invoice.expenses:
            vat_kr = exp.amount_excl_vat * (exp.vat_rate / 100)
            desc = str(exp.expense_date) + "  " + (exp.description or exp.merchant or "Utlägg")
            rows.append([
                td(desc),
                tdr("1"),
                td("st" if sv else "pcs"),
                tdr(sek(exp.amount_excl_vat)),
                tdr(f"{int(exp.vat_rate)}%"),
                tdr(sek(vat_kr)),
                tdr(sek(exp.amount_excl_vat)),
            ])

    n = len(rows)
    ts = TableStyle([
        # Header
        ("LINEBELOW",     (0, 0), (-1, 0), 1.0, C_BLACK),
        ("TOPPADDING",    (0, 0), (-1, 0), 3),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
        # Body
        ("TOPPADDING",    (0, 1), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
        ("LINEBELOW",     (0, 1), (-1, -1), 0.4, C_RULE),
        # Right-align columns 1,3,4,5,6
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ALIGN", (3, 0), (6, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        # Stripe even data rows
        *[("BACKGROUND", (0, r), (-1, r), C_STRIPE)
          for r in range(2, n, 2)],
    ])
    # Style expense section headers
    for ri in row_colors:
        ts.add("BACKGROUND", (0, ri), (-1, ri), C_WHITE)
        ts.add("LINEABOVE",  (0, ri), (-1, ri), 0.6, C_RULE)
        ts.add("LINEBELOW",  (0, ri), (-1, ri), 0.6, C_RULE)

    items_table = Table(rows, colWidths=COLS, style=ts, repeatRows=1)
    story.append(items_table)
    story.append(Spacer(1, 4*mm))

    # ── Totals block (right-aligned) ──
    summary_left = []
    summary_left.append(Paragraph(
        ("Arbete upparbetat t.o.m " if sv else "Work performed through ") +
        str(invoice.period_end), s["small"]))
    summary_left.append(Paragraph(
        fmt_hours(ctx["total_hours"]) + " h", s["small"]))

    totals_data = [
        [Paragraph("Belopp före moms" if sv else "Subtotal", s["small"]),
         Paragraph(sek(invoice.subtotal), s["small_b"])],
        [Paragraph("Moms 25%" if sv else "VAT 25%", s["small"]),
         Paragraph(sek(invoice.vat_amount), s["small_b"])],
    ]
    if abs(ctx["rounding"]) >= 0.005:
        totals_data.append([
            Paragraph("Öresavrundning" if sv else "Rounding", s["small"]),
            Paragraph(sek(ctx["rounding"]), s["small_b"]),
        ])
    totals_data.append([
        Paragraph("Att betala" if sv else "Amount due", s["cell_b"]),
        Paragraph(sek(invoice.total), s["cell_b"]),
    ])

    n_tot = len(totals_data)
    totals_ts = TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEABOVE",  (0, n_tot-1), (-1, n_tot-1), 1.0, C_BLACK),
        ("LINEBELOW",  (0, 0),       (-1, 0),        0.4, C_RULE),
    ])
    totals_table = Table(totals_data, colWidths=[34*mm, 28*mm], style=totals_ts)

    bottom_table = Table(
        [[summary_left, totals_table]],
        colWidths=[100*mm, 62*mm],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN",  (1, 0), (1,  0),  "RIGHT"),
        ]),
    )
    story.append(bottom_table)

    if invoice.notes:
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(invoice.notes, s["small"]))

    return story


# ── Timesheet PDF ─────────────────────────────────────────────────────────────

def _build_timesheet_story(invoice, ctx, s):
    sv = (ctx["lang"] == "sv")
    story = []

    story.append(Paragraph(
        ("Tidrapport" if sv else "Timesheet") +
        f" – {invoice.client.name}  {invoice.period_start} – {invoice.period_end}",
        s["cell_b"]))
    story.append(Spacer(1, 3*mm))

    COLS = [25*mm, 20*mm, 80*mm, 18*mm]
    for pid, group in ctx["by_project"].items():
        proj = group["project"]
        hdr = [
            [Paragraph(f"{proj.client.name} / {proj.name}", s["cell_b"]),
             "", "", ""],
            [Paragraph("Datum" if sv else "Date", s["small_b"]),
             Paragraph("Typ" if sv else "Type", s["small_b"]),
             Paragraph("Beskrivning" if sv else "Description", s["small_b"]),
             Paragraph("Timmar" if sv else "Hours", s["small_b"])],
        ]
        rows = hdr[:]
        for e in group["entries"]:
            rows.append([
                Paragraph(str(e.entry_date), s["small"]),
                Paragraph(e.display_type or "", s["small"]),
                Paragraph(e.description or "–", s["small"]),
                Paragraph(fmt_hours(e.hours), s["small"]),
            ])
        rows.append([
            Paragraph("Summa" if sv else "Total", s["small_b"]),
            "", "",
            Paragraph(fmt_hours(group["total"]) + " h", s["small_b"]),
        ])

        n = len(rows)
        ts = TableStyle([
            ("SPAN",          (0, 0), (-1, 0)),
            ("BACKGROUND",    (0, 0), (-1, 0), C_BLACK),
            ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LINEBELOW",     (0, 1), (-1, 1), 0.8, C_BLACK),
            ("LINEBELOW",     (0, 2), (-1, -2), 0.4, C_RULE),
            ("BACKGROUND",    (0, n-1), (-1, n-1), C_STRIPE),
            ("ALIGN",         (3, 0), (3, -1), "RIGHT"),
        ])
        story.append(Table(rows, colWidths=COLS, style=ts, repeatRows=2))
        story.append(Spacer(1, 4*mm))

    # Rate summary
    summary_rows = [[
        Paragraph("Timtyp / Rate" if sv else "Hour type / Rate", s["small_b"]),
        Paragraph("Timmar" if sv else "Hours", s["small_b"]),
        Paragraph("Belopp" if sv else "Amount", s["small_b"]),
    ]]
    for row in ctx["rate_summary"]:
        summary_rows.append([
            Paragraph(row["label"], s["small"]),
            Paragraph(fmt_hours(row["hours"]) + " h", s["small"]),
            Paragraph(sek(row["hours"] * row["rate"]), s["small"]),
        ])
    story.append(Table(summary_rows, colWidths=[80*mm, 30*mm, 33*mm],
                       style=TableStyle([
                           ("LINEBELOW",     (0, 0), (-1, 0), 0.8, C_BLACK),
                           ("TOPPADDING",    (0, 0), (-1, -1), 2),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                           ("ALIGN",         (2, 0), (2, -1), "RIGHT"),
                       ])))
    return story


# ── Public API ────────────────────────────────────────────────────────────────

def generate_invoice_pdf(invoice, config) -> str:
    """Generate invoice PDF (invoice + timesheet) and return path."""
    upload_folder = os.path.join(os.path.dirname(__file__), "static", "uploads")
    os.makedirs(upload_folder, exist_ok=True)

    output_filename = f"invoice_{invoice.invoice_number.replace('-', '_')}.pdf"
    output_path = os.path.join(upload_folder, output_filename)

    ctx = _ctx(invoice, config)
    s   = _styles()

    W, H = A4
    lm = 15 * mm
    rm = 15 * mm
    tm = 14 * mm
    bm = 28 * mm  # leave room for footer

    footer_cb = _make_footer_cb(ctx["company"], ctx["lang"])

    # Page 1 frame (invoice)
    inv_frame = Frame(lm, bm, W - lm - rm, H - tm - bm, id="inv",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    # Page 2+ frame (timesheet) — same margins
    ts_frame  = Frame(lm, bm, W - lm - rm, H - tm - bm, id="ts",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    doc = BaseDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=lm, rightMargin=rm, topMargin=tm, bottomMargin=bm,
    )
    inv_tpl = PageTemplate(id="invoice",   frames=[inv_frame], onPage=footer_cb)
    ts_tpl  = PageTemplate(id="timesheet", frames=[ts_frame],  onPage=footer_cb)
    doc.addPageTemplates([inv_tpl, ts_tpl])

    from reportlab.platypus import NextPageTemplate, PageBreak

    story  = _build_invoice_story(invoice, ctx, s)
    story += [NextPageTemplate("timesheet"), PageBreak()]
    story += _build_timesheet_story(invoice, ctx, s)

    doc.build(story)
    return output_path


def render_invoice_html(invoice, config) -> str:
    """Browser print fallback — generates PDF and returns a redirect page."""
    # With ReportLab we always generate a real PDF; the browser route
    # just serves the PDF inline so the user can print from there.
    path = generate_invoice_pdf(invoice, config)
    filename = os.path.basename(path)
    url = f"/static/uploads/{filename}"
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>Invoice</title></head><body>'
        f'<script>window.location="{url}";</script>'
        f'<p><a href="{url}">Open PDF</a></p>'
        f'</body></html>'
    )
