"""
Invoice PDF generator using ReportLab.
Font: Helvetica 10pt throughout. No borders except header rule and address separator.
Totals pinned to bottom of page above footer via fixed frame.
"""
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer,
    Table, TableStyle, NextPageTemplate, PageBreak, FrameBreak,
)
from reportlab.platypus.flowables import HRFlowable

# ── Palette ───────────────────────────────────────────────────────────────────
C_BLACK = colors.HexColor("#1a1a1a")
C_WHITE = colors.white
C_RULE  = colors.HexColor("#cccccc")

FONT  = "Helvetica"
FONTB = "Helvetica-Bold"
SZ     = 10
SZ_BIG = 14

PW       = 180 * mm
TOTALS_H = 50 * mm


# ── Number formatting ─────────────────────────────────────────────────────────

def sek(val):
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
    s = {}
    s["n"]       = ParagraphStyle("n",       fontName=FONT,  fontSize=SZ,     textColor=C_BLACK, leading=SZ * 1.45)
    s["b"]       = ParagraphStyle("b",       fontName=FONTB, fontSize=SZ,     textColor=C_BLACK, leading=SZ * 1.45)
    s["big_b"]   = ParagraphStyle("big_b",   fontName=FONTB, fontSize=SZ_BIG, textColor=C_BLACK, leading=SZ_BIG * 1.3)
    s["inv"]     = ParagraphStyle("inv",     fontName=FONTB, fontSize=20,     textColor=C_BLACK, leading=24)
    s["company"] = ParagraphStyle("company", fontName=FONTB, fontSize=13,     textColor=C_BLACK, leading=16)
    s["nr"]      = ParagraphStyle("nr",      fontName=FONT,  fontSize=SZ,     textColor=C_BLACK, leading=SZ * 1.45,    alignment=2)
    s["br"]      = ParagraphStyle("br",      fontName=FONTB, fontSize=SZ,     textColor=C_BLACK, leading=SZ * 1.45,    alignment=2)
    s["big_br"]  = ParagraphStyle("big_br",  fontName=FONTB, fontSize=SZ_BIG, textColor=C_BLACK, leading=SZ_BIG * 1.3, alignment=2)
    s["inv_r"]   = ParagraphStyle("inv_r",   fontName=FONTB, fontSize=20,     textColor=C_BLACK, leading=24,           alignment=2)
    return s


def _p0():
    return [
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]


# ── Footer ────────────────────────────────────────────────────────────────────

def _make_footer_cb(company, lang):
    def _footer(canvas, doc):
        canvas.saveState()
        lm    = 15 * mm
        y     = 8 * mm
        col   = PW / 4
        shift = 5 * mm
        LINE_GAP = 4.2 * mm

        canvas.setStrokeColor(C_RULE)
        canvas.setLineWidth(1.2)
        canvas.line(lm, y + 19 * mm, lm + PW, y + 19 * mm)

        sv = (lang == "sv")

        def block(x, title, lines):
            canvas.setFont(FONTB, 7)
            canvas.setFillColor(C_BLACK)
            canvas.drawString(x, y + 14 * mm, title)
            cy = y + 14 * mm - LINE_GAP
            for line in lines:
                if not line:
                    cy -= LINE_GAP
                    continue
                canvas.setFont(FONT, SZ - 1)
                canvas.setFillColor(C_BLACK)
                canvas.drawString(x, cy, str(line))
                cy -= LINE_GAP

        block(lm,
              "Adress" if sv else "Address",
              [company["name"], company["address"]])
        block(lm + col + shift,
              "Kontakt" if sv else "Contact",
              [company.get("phone", ""), company.get("email", "")])
        block(lm + 2 * col + shift,
              "Bankgiro",
              [company.get("bankgiro", ""),
               "Godkänd för F-skatt" if sv else "Approved for F-tax"])
        block(lm + 3 * col + shift,
              "Org.nr / VAT",
              [company.get("org_nr", ""), company.get("vat_nr", "")])

        canvas.restoreState()
    return _footer


# ── Context ───────────────────────────────────────────────────────────────────

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
    logo_path   = os.path.join(os.path.dirname(__file__), config["COMPANY_LOGO_PATH"])
    logo_exists = os.path.exists(logo_path)
    entries     = sorted(invoice.time_entries, key=lambda e: e.entry_date)
    total_hours = sum(e.hours for e in entries)
    po_numbers  = ", ".join(sorted(set(
        e.hour_type.po.po_number
        for e in entries
        if e.hour_type and e.hour_type.po and e.hour_type.po.po_number
    )))
    project_numbers = ", ".join(sorted(set(
        e.project.project_number
        for e in entries
        if e.project and e.project.project_number
    )))
    # rounding = difference between stored whole-kronor total and exact subtotal+vat
    rounding = round(invoice.total - invoice.subtotal - invoice.vat_amount, 2)
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
        key  = (e.project_id, e.hour_type_id, rate)
        if key not in rate_groups:
            lp = [e.project.name]
            if e.display_type and e.display_type != "Normal":
                lp.append(e.display_type)
            rate_groups[key] = {"label": " – ".join(lp), "hours": 0.0, "rate": rate}
        rate_groups[key]["hours"] += e.hours
    return dict(
        lang=lang, company=company, logo_path=logo_path, logo_exists=logo_exists,
        entries=entries, total_hours=total_hours, po_numbers=po_numbers,
        project_numbers=project_numbers,
        rounding=rounding, by_project=by_project,
        rate_summary=list(rate_groups.values()),
    )


# ── Invoice content ───────────────────────────────────────────────────────────

def _build_invoice_story(invoice, ctx, s):
    sv    = (ctx["lang"] == "sv")
    co    = ctx["company"]
    story = []

    # ── Header ──
    if ctx["logo_exists"]:
        from reportlab.platypus import Image
        logo_cell = Image(ctx["logo_path"], width=45*mm, height=14*mm, kind="proportional")
    else:
        logo_cell = Paragraph(co["name"], s["company"])

    inv_meta_rows = [
        [Paragraph("Fakturanr"      if sv else "Invoice No.",    s["nr"]),
         Paragraph(str(invoice.invoice_number),                  s["br"])],
        [Paragraph("Fakturadatum"   if sv else "Invoice date",   s["nr"]),
         Paragraph(str(invoice.issue_date),                      s["nr"])],
    ]
    if ctx["project_numbers"]:
        inv_meta_rows.append([
            Paragraph("Uppdragsnr" if sv else "Assignment No.", s["nr"]),
            Paragraph(ctx["project_numbers"],                   s["br"]),
        ])
    inv_meta = Table(inv_meta_rows, colWidths=[38*mm, 32*mm], style=TableStyle([
        *_p0(),
    ]))

    story.append(Table(
        [[logo_cell,
          Table([[Paragraph("FAKTURA" if sv else "INVOICE", s["inv_r"])],
                 [inv_meta]], colWidths=[70*mm],
                style=TableStyle(_p0()))]],
        colWidths=[PW - 70*mm, 70*mm],
        style=TableStyle([
            *_p0(),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("ALIGN",         (1, 0), (1,  0),  "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]),
    ))
    story.append(Spacer(1, 4*mm))

    # ── Address + thin rule below ──
    client     = invoice.client
    addr_lines = [Paragraph("Fakturaadress" if sv else "Invoice address", s["b"]),
                  Paragraph(client.name, s["b"])]
    if client.address:
        for line in client.address.split("\n"):
            if line.strip():
                addr_lines.append(Paragraph(line.strip(), s["n"]))
    if client.contact_email:
        addr_lines.append(Paragraph(client.contact_email, s["n"]))

    story.append(Table(
        [[addr_lines, ""]],
        colWidths=[PW / 2, PW / 2],
        style=TableStyle([
            *_p0(),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.5, C_RULE),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]),
    ))
    story.append(Spacer(1, 4*mm))

    # ── Meta grid ──
    def mrow(k, v):
        return [Paragraph(k, s["n"]), Paragraph(str(v) if v else "–", s["b"])]

    half = PW / 2
    left_meta = [
        mrow("Org.nr"           if sv else "Org. No.",       client.org_nr or "–"),
        mrow("Er referens"     if sv else "Your ref.",      client.contact_name or "–"),
        mrow("Ert ordernr"     if sv else "Your order No.", ctx["po_numbers"] or "–"),
        mrow("Ert VAT-nr"      if sv else "Your VAT No.",   client.vat_nr or "–"),
    ]
    right_meta = [
        mrow("Vår referens"      if sv else "Our ref.",       co["reference"] or "–"),
        mrow("Betalningsvillkor" if sv else "Payment terms",
             f"{client.payment_days} {'dagar' if sv else 'days'}"),
        mrow("Förfallodatum"     if sv else "Due date",       str(invoice.due_date)),
        mrow("Leveransdatum"     if sv else "Delivery date",  str(invoice.period_end)),
    ]
    meta_ts = TableStyle([
        *_p0(),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ])
    story.append(Table(
        [[Table(left_meta,  colWidths=[40*mm, half - 40*mm], style=meta_ts),
          Table(right_meta, colWidths=[40*mm, half - 40*mm], style=meta_ts)]],
        colWidths=[half, half],
        style=TableStyle([*_p0(), ("VALIGN", (0, 0), (-1, -1), "TOP")]),
    ))
    story.append(Spacer(1, 5*mm))

    # ── Line items ──
    # Desc, Qty, Unit, UnitPrice, VAT%, VATamt, Amount — sum = 180mm
    COLS = [73*mm, 12*mm, 14*mm, 23*mm, 13*mm, 21*mm, 24*mm]

    def td(txt):  return Paragraph(str(txt), s["n"])
    def tdb(txt): return Paragraph(str(txt), s["b"])

    rows = [[
        tdb("Beskrivning"  if sv else "Description"),
        tdb("Antal"        if sv else "Qty"),
        tdb("Enhet"        if sv else "Unit"),
        tdb("à pris"       if sv else "Unit price"),
        tdb("Moms"         if sv else "VAT"),
        tdb("Moms kr"      if sv else "VAT amt"),
        tdb("Belopp"       if sv else "Amount"),
    ]]

    # One row per (year, month, hour type) — uses Invoice.line_groups()
    month_names_sv = ["Jan","Feb","Mar","Apr","Maj","Jun","Jul","Aug","Sep","Okt","Nov","Dec"]
    month_names_en = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    mnames = month_names_sv if sv else month_names_en

    expense_sec_rows = []
    for group in invoice.line_groups():
        mo_str = f"{group['year']} {mnames[group['month'] - 1]}"
        if group["show_po"] and group["po_number"]:
            mo_str += f" ({group['po_number']})"
        desc   = f"{mo_str}  {group['hour_type_name']}"
        amount = group["amount"]
        vat_kr = round(amount * 0.25, 2)
        rows.append([td(desc), td(fmt_hours(group["hours"])), td("tim" if sv else "hr"),
                     td(sek(group["rate"])), td("25%"), td(sek(vat_kr)), td(sek(amount))])

    if invoice.expenses:
        expense_sec_rows.append(len(rows))
        rows.append([tdb("Utlägg" if sv else "Expenses"), "", "", "", "", "", ""])
        for exp in invoice.expenses:
            vat_kr = exp.amount_excl_vat * (exp.vat_rate / 100)
            desc   = str(exp.expense_date) + "   " + (exp.description or exp.merchant or "Utlägg")
            rows.append([td(desc), td("1"), td("st" if sv else "pcs"),
                         td(sek(exp.amount_excl_vat)), td(f"{int(exp.vat_rate)}%"),
                         td(sek(vat_kr)), td(sek(exp.amount_excl_vat))])

    if invoice.mileage_entries:
        expense_sec_rows.append(len(rows))
        rows.append([tdb("Reseersättning" if sv else "Mileage"), "", "", "", "", "", ""])
        # Group mileage by (year, month, project)
        mile_groups = {}
        for m in sorted(invoice.mileage_entries, key=lambda x: x.entry_date):
            yr, mo = m.entry_date.year, m.entry_date.month
            key = (yr, mo, m.project_id, m.km_rate)
            if key not in mile_groups:
                mile_groups[key] = {"year": yr, "month": mo,
                                    "project_name": m.project.name if m.project else "",
                                    "km_rate": m.km_rate, "km": 0.0}
            mile_groups[key]["km"] += m.km
        for key in sorted(mile_groups.keys()):
            g2 = mile_groups[key]
            desc   = f"{g2['year']} {month_names[g2['month'] - 1]}  {g2['project_name']}"
            amount = g2["km"] * g2["km_rate"]
            vat_kr = amount * 0.25
            rows.append([td(desc), td(f"{g2['km']:.0f}"), td("km"),
                         td(sek(g2["km_rate"])), td("25%"), td(sek(vat_kr)), td(sek(amount))])

    items_ts = TableStyle([
        ("LINEBELOW",     (0, 0), (-1,  0), 0.75, C_BLACK),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        ("ALIGN",         (1, 0), (1,  -1), "RIGHT"),
        ("ALIGN",         (3, 0), (6,  -1), "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ])
    for ri in expense_sec_rows:
        items_ts.add("TOPPADDING", (0, ri), (-1, ri), 7)

    story.append(Table(rows, colWidths=COLS, style=items_ts, repeatRows=1))
    story.append(FrameBreak())
    return story


# ── Totals block ──────────────────────────────────────────────────────────────

def _build_totals_story(invoice, ctx, s):
    sv    = (ctx["lang"] == "sv")
    story = []

    story.append(HRFlowable(width="100%", thickness=0.75, color=C_BLACK, spaceAfter=3*mm))

    summary_left = [
        Paragraph(("Arbete upparbetat t.o.m " if sv else "Work performed through ") +
                  str(invoice.period_end), s["n"]),
        Paragraph(fmt_hours(ctx["total_hours"]) + " tim", s["n"]),
    ]

    totals_data = [
        [Paragraph("Belopp före moms" if sv else "Subtotal", s["n"]),
         Paragraph(sek(invoice.subtotal),   s["br"])],
        [Paragraph("Moms 25%"          if sv else "VAT 25%", s["n"]),
         Paragraph(sek(invoice.vat_amount), s["br"])],
    ]
    if abs(ctx["rounding"]) >= 0.005:
        totals_data.append([
            Paragraph("Öresavrundning" if sv else "Rounding", s["n"]),
            Paragraph(sek(ctx["rounding"]), s["br"]),
        ])
    nt = len(totals_data)
    totals_data.append([
        Paragraph("Att betala" if sv else "Amount due", s["big_b"]),
        Paragraph(sek(invoice.total),                   s["big_br"]),
    ])
    totals_data.append([
        Paragraph("Bankgiro", s["big_b"]),
        Paragraph(ctx["company"]["bankgiro"], s["big_br"]),
    ])

    totals_ts = TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("ALIGN",         (1, 0), (1,  -1), "RIGHT"),
        ("LINEABOVE",     (0, nt), (-1, nt), 0.75, C_BLACK),
    ])

    story.append(Table(
        [[summary_left, Table(totals_data, colWidths=[38*mm, 32*mm], style=totals_ts)]],
        colWidths=[PW - 70*mm, 70*mm],
        style=TableStyle([
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("ALIGN",         (1, 0), (1,  0),  "RIGHT"),
        ]),
    ))

    if invoice.notes:
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(invoice.notes, s["n"]))

    return story


# ── Timesheet ─────────────────────────────────────────────────────────────────

def _build_timesheet_story(invoice, ctx, s):
    sv    = (ctx["lang"] == "sv")
    story = []

    story.append(Paragraph(
        ("Tidrapport" if sv else "Timesheet") +
        f" – {invoice.client.name}  {invoice.period_start} – {invoice.period_end}",
        s["b"]))
    story.append(Spacer(1, 4*mm))

    COLS = [28*mm, 22*mm, PW - 28*mm - 22*mm - 22*mm, 22*mm]
    for _, group in ctx["by_project"].items():
        proj = group["project"]
        rows = [
            [Paragraph(f"{proj.client.name} / {proj.name}", s["b"]), "", "", ""],
            [Paragraph("Datum"       if sv else "Date",        s["b"]),
             Paragraph("Typ"         if sv else "Type",        s["b"]),
             Paragraph("Beskrivning" if sv else "Description", s["b"]),
             Paragraph("Tim"         if sv else "Hrs",         s["b"])],
        ]
        for e in group["entries"]:
            rows.append([
                Paragraph(str(e.entry_date),              s["n"]),
                Paragraph(e.display_type or "",           s["n"]),
                Paragraph(e.description or "–",           s["n"]),
                Paragraph(fmt_hours(e.hours) + " tim",    s["n"]),
            ])
        n = len(rows)
        rows.append([
            Paragraph("Summa" if sv else "Total", s["b"]),
            "", "",
            Paragraph(fmt_hours(group["total"]) + " tim", s["b"]),
        ])
        ts = TableStyle([
            ("SPAN",          (0, 0),   (-1, 0)),
            ("LINEBELOW",     (0, 1),   (-1,  1),  0.75, C_BLACK),
            ("LINEABOVE",     (0, n),   (-1,  n),  0.5,  C_RULE),
            ("TOPPADDING",    (0, 0),   (-1, -1),  2),
            ("BOTTOMPADDING", (0, 0),   (-1, -1),  2),
            ("LEFTPADDING",   (0, 0),   (-1, -1),  0),
            ("RIGHTPADDING",  (0, 0),   (-1, -1),  2),
            ("ALIGN",         (3, 0),   (3,  -1),  "RIGHT"),
            ("VALIGN",        (0, 0),   (-1, -1),  "TOP"),
        ])
        story.append(Table(rows, colWidths=COLS, style=ts, repeatRows=2))
        story.append(Spacer(1, 5*mm))

    # Mileage section
    if invoice.mileage_entries:
        story.append(Paragraph("Reseersättning" if sv else "Mileage", s["b"]))
        story.append(Spacer(1, 3*mm))
        MCOLS = [28*mm, PW - 28*mm - 22*mm - 22*mm, 22*mm, 22*mm]
        mile_rows = [
            [Paragraph("Datum" if sv else "Date", s["b"]),
             Paragraph("Beskrivning" if sv else "Description", s["b"]),
             Paragraph("Km", s["b"]),
             Paragraph("Kr/km", s["b"])],
        ]
        for m in sorted(invoice.mileage_entries, key=lambda x: x.entry_date):
            mile_rows.append([
                Paragraph(str(m.entry_date), s["n"]),
                Paragraph(m.description or "–", s["n"]),
                Paragraph(f"{m.km:.0f}", s["n"]),
                Paragraph(fmt_hours(m.km_rate), s["n"]),
            ])
        story.append(Table(mile_rows, colWidths=MCOLS, style=TableStyle([
            ("LINEBELOW",     (0, 0), (-1, 0),  0.75, C_BLACK),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
            ("ALIGN",         (2, 0), (3, -1),  "RIGHT"),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ])))

    return story


# ── Public API ────────────────────────────────────────────────────────────────

def generate_invoice_pdf(invoice, config) -> str:
    upload_folder = os.path.join(os.path.dirname(__file__), "static", "uploads")
    os.makedirs(upload_folder, exist_ok=True)
    output_filename = f"invoice_{invoice.invoice_number.replace('-', '_')}.pdf"
    output_path     = os.path.join(upload_folder, output_filename)

    ctx = _ctx(invoice, config)
    s   = _styles()

    _, H = A4
    lm = 15 * mm
    rm = 15 * mm
    tm = 14 * mm
    bm = 36 * mm

    footer_cb = _make_footer_cb(ctx["company"], ctx["lang"])

    content_frame = Frame(
        lm, bm + TOTALS_H, PW, H - tm - bm - TOTALS_H,
        id="inv_content",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    totals_frame = Frame(
        lm, bm, PW, TOTALS_H,
        id="inv_totals",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    ts_frame = Frame(
        lm, bm, PW, H - tm - bm,
        id="ts",
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )

    doc = BaseDocTemplate(
        output_path, pagesize=A4,
        leftMargin=lm, rightMargin=rm, topMargin=tm, bottomMargin=bm,
    )
    doc.addPageTemplates([
        PageTemplate(id="invoice",   frames=[content_frame, totals_frame], onPage=footer_cb),
        PageTemplate(id="timesheet", frames=[ts_frame],                    onPage=footer_cb),
    ])

    story  = _build_invoice_story(invoice, ctx, s)
    story += _build_totals_story(invoice, ctx, s)
    story += [NextPageTemplate("timesheet"), PageBreak()]
    story += _build_timesheet_story(invoice, ctx, s)

    doc.build(story)
    return output_path


def render_invoice_html(invoice, config) -> str:
    path     = generate_invoice_pdf(invoice, config)
    filename = os.path.basename(path)
    url      = f"/static/uploads/{filename}"
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>Invoice</title></head>'
        f'<body><script>window.location="{url}";</script>'
        f'<p><a href="{url}">Open PDF</a></p></body></html>'
    )
