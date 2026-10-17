"""
Lönebesked PDF (payslip) — ReportLab, same typography as the invoice PDF.
Returns bytes; the caller decides whether to store, download or email it.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.platypus.flowables import HRFlowable

from pdf_generator import C_BLACK, C_RULE, FONT, FONTB, PW, SZ, sek, _styles, _p0
from payroll import kind_sign, payslip_summary

MONTHS_SV = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
             "augusti", "september", "oktober", "november", "december"]


def period_label(period):
    """"2026-06" → "juni 2026"."""
    try:
        return f"{MONTHS_SV[int(period[5:7]) - 1]} {period[:4]}"
    except (ValueError, IndexError):
        return period


def _num(val):
    if val in (None, ""):
        return ""
    v = float(val)
    return (str(int(v)) if v == int(v) else f"{v:.2f}").replace(".", ",")


def _ytd_footer(ytd, year, through):
    """Ackumulerat pinned to the bottom of the page, laid out like the invoice footer."""
    def _footer(canvas, doc):
        canvas.saveState()
        lm, y, col = doc.leftMargin, 8 * mm, PW / 4
        canvas.setStrokeColor(C_RULE)
        canvas.setLineWidth(1.2)
        canvas.line(lm, y + 19 * mm, lm + PW, y + 19 * mm)
        blocks = [(f"Ackumulerat {year}", f"t.o.m. {through}"),
                  ("Skattepliktig bruttolön", sek(ytd.get("gross", 0))),
                  ("varav förmåner", sek(ytd.get("benefits", 0))),
                  ("Avdragen skatt", sek(ytd.get("tax", 0)))]
        for i, (title, value) in enumerate(blocks):
            x = lm + i * col
            canvas.setFillColor(C_BLACK)
            canvas.setFont(FONTB, 7)
            canvas.drawString(x, y + 14 * mm, title)
            canvas.setFont(FONT, SZ - 1)
            canvas.drawString(x, y + 14 * mm - 4.2 * mm, value)
        canvas.restoreState()
    return _footer


def generate_payslip_pdf(payslip, config, ytd=None) -> bytes:
    """ytd: {"gross", "tax", "benefits"} for the calendar year up to and including this slip."""
    s = _styles()
    emp = payslip.employee
    summary = payslip_summary(payslip)
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                          topMargin=14 * mm, bottomMargin=32 * mm,
                          title=f"Lönebesked {emp.name} {payslip.period}")
    # Zero-padding frame, as in the invoice PDF, so full-width (PW) tables line up.
    footer = {"onPage": _ytd_footer(ytd, payslip.pay_date.year, payslip.pay_date.isoformat())} if ytd else {}
    doc.addPageTemplates([PageTemplate(id="slip", frames=[Frame(
        doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)],
        **footer)])
    story = []

    company_lines = [config.get("COMPANY_NAME", "")]
    company_lines += [l.strip() for l in (config.get("COMPANY_ADDRESS") or "").splitlines() if l.strip()]
    if config.get("COMPANY_ORG_NR"):
        company_lines.append(f"Org.nr {config['COMPANY_ORG_NR']}")
    emp_lines = [emp.name, emp.address or "", f"{emp.postal_code or ''} {emp.city or ''}".strip()]

    head = Table([[
        [Paragraph(company_lines[0], s["company"])] + [Paragraph(l, s["n"]) for l in company_lines[1:]],
        [Paragraph("LÖNEBESKED", s["inv_r"]), Spacer(1, 6 * mm)]
        + [Paragraph(l, s["nr"]) for l in emp_lines if l],
    ]], colWidths=[PW / 2, PW / 2])
    head.setStyle(TableStyle(_p0() + [("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 8 * mm)]

    info = [
        ("Avser perioden", period_label(payslip.period)),
        ("Utbetalningsdag", payslip.pay_date.isoformat()),
        ("Personnummer", emp.personnummer_display),
        ("Konto", emp.bank_display),
    ]
    if emp.tax_table:
        info.append(("Skattetabell", emp.tax_table))
    info_tbl = Table([[Paragraph(k, s["b"]), Paragraph(v, s["n"])] for k, v in info],
                     colWidths=[40 * mm, PW - 40 * mm])
    info_tbl.setStyle(TableStyle(_p0() + [("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [info_tbl, Spacer(1, 6 * mm),
              HRFlowable(width="100%", thickness=1.2, color=C_RULE), Spacer(1, 2 * mm)]

    rows = [[Paragraph(h, s["b"]) for h in ("Löneart", "Benämning")]
            + [Paragraph(h, s["br"]) for h in ("Antal", "À-pris", "Belopp")]]
    for line in payslip.lines:
        st = line.salary_type
        amount = float(line.amount or 0) * kind_sign(st.kind)
        rows.append([
            Paragraph(st.code, s["n"]),
            Paragraph(line.description or st.name, s["n"]),
            Paragraph(f"{_num(line.quantity)} {st.unit or ''}".strip() if line.quantity else "", s["nr"]),
            Paragraph(_num(line.rate), s["nr"]),
            Paragraph(sek(amount), s["nr"]),
        ])
    lines_tbl = Table(rows, colWidths=[18 * mm, PW - 18 * mm - 25 * mm - 25 * mm - 32 * mm, 25 * mm, 25 * mm, 32 * mm],
                      repeatRows=1)
    lines_tbl.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, C_RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [lines_tbl, Spacer(1, 6 * mm)]

    totals = [("Kontant bruttolön", summary["cash_gross"])]
    if summary["benefits"]:
        totals.append(("Skattepliktiga förmåner", summary["benefits"]))
    totals.append(("Skatteavdrag", -summary["tax"]))
    if summary["tax_free"]:
        totals.append(("Skattefria ersättningar", summary["tax_free"]))
    if summary["net_deduct"]:
        totals.append(("Nettolöneavdrag", -summary["net_deduct"]))
    tot_rows = [[Paragraph(k, s["n"]), Paragraph(sek(v), s["nr"])] for k, v in totals]
    tot_rows.append([Paragraph("Att utbetala", s["big_b"]), Paragraph(sek(summary["net_pay"]), s["big_br"])])
    tot_tbl = Table(tot_rows, colWidths=[PW - 50 * mm, 50 * mm], hAlign="RIGHT")
    tot_tbl.setStyle(TableStyle(_p0() + [
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEABOVE", (0, -1), (-1, -1), 1.2, C_BLACK),
        ("TOPPADDING", (0, -1), (-1, -1), 5),
    ]))
    story.append(tot_tbl)

    if payslip.note:
        story += [Spacer(1, 6 * mm), Paragraph(payslip.note, s["n"])]

    doc.build(story)
    return buf.getvalue()
