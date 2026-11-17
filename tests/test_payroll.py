"""
Lönebesked: calculation, AGI file, payment file and the two vouchers.

The reference case is the June 2026 salary as it was hand-booked in Fortnox:
  1930 K 47 547 · 7220 D 69 933 · 2710 K 22 386 · 7330/7399 122 and 800 · 7510/2731 22 263
"""
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pytest

import app as app_module
from models import (Employee, Payslip, PayslipLine, SalaryType, AgiFieldCode, AgiReport,
                    PaymentFile, SupplierInvoice)
from payroll import (salary_voucher_rows, voucher_balanced, payslip_agi_values, agi_totals,
                     build_agi_xml, tax_payment_rows, tax_settlement_rows, net_pay,
                     normalize_personnummer, payslip_summary, mileage_over_limit,
                     add_month, next_period, skv_due_date)

PAIN = "{urn:iso:std:iso:20022:tech:xsd:pain.001.001.03}"
AGD = "{http://xmls.skatteverket.se/se/skatteverket/da/komponent/schema/1.1}"


@pytest.fixture
def seeded(db):
    app_module._seed_payroll()
    return {st.code: st for st in SalaryType.query.all()}


@pytest.fixture
def staffan(db):
    e = Employee(first_name="Staffan", last_name="Edlund", personnummer="197901197512",
                 address="Tryffelvägen 4C", postal_code="756 46", city="Uppsala",
                 email="staffan@example.com", bank_clearing="3300", bank_account="7901197512",
                 salary_account="7220", ag_rate=0.3142, tax_table="34")
    db.session.add(e)
    db.session.commit()
    return e


def _june_slip(db, employee, types, pay_date=date(2026, 6, 25)):
    p = Payslip(employee_id=employee.id, period=pay_date.strftime("%Y-%m"), pay_date=pay_date,
                spec_nr=1, status="draft")
    for i, (code, desc, amount) in enumerate([
        ("100", "Lön", 70000),
        ("110", "Bruttolöneavdrag Lease a bike", 66.58),
        ("200", "Cykelförmån Lease a bike", 122.04),
        ("200", "Cykelförmån Santa Cruz Megatower", 800),
        ("900", "Skatt enligt tabell 34", 22386),
    ]):
        p.lines.append(PayslipLine(salary_type_id=types[code].id, description=desc, amount=amount, sort_order=i))
    db.session.add(p)
    db.session.commit()
    return p


class FakeFortnox:
    posted = []

    def __init__(self, config):
        pass

    def create_voucher(self, rows, description, voucher_date, series="A"):
        FakeFortnox.posted.append({"rows": rows, "description": description,
                                   "date": voucher_date, "series": series})
        return f"{voucher_date.year}-{series}{len(FakeFortnox.posted)}"

    def create_payment_voucher(self, invoice_ids, payment_date):
        FakeFortnox.posted.append({"supplier": invoice_ids, "date": payment_date})
        return f"{payment_date.year}-A{len(FakeFortnox.posted)}"


@pytest.fixture
def fortnox(monkeypatch):
    FakeFortnox.posted = []
    monkeypatch.setattr(app_module, "FortnoxClient", FakeFortnox)
    return FakeFortnox


# ── Calculation ───────────────────────────────────────────────────────────────

def test_salary_voucher_matches_fortnox_booking(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    rows = [(r["account"], r["debit"], r["credit"], r["info"]) for r in salary_voucher_rows(p)]
    assert rows == [
        ("1930", 0, 47547, ""),
        ("7220", 69933, 0, ""),
        ("2710", 0, 22386, ""),
        ("7330", 122, 0, "Cykelförmån Lease a bike"),
        ("7399", 0, 122, "Cykelförmån Lease a bike"),
        ("7330", 800, 0, "Cykelförmån Santa Cruz Megatower"),
        ("7399", 0, 800, "Cykelförmån Santa Cruz Megatower"),
        ("7510", 22263, 0, "Arbetsgivaravgifter 31,42 %"),
        ("2731", 0, 22263, "Arbetsgivaravgifter 31,42 %"),
    ]
    assert voucher_balanced(salary_voucher_rows(p))
    assert net_pay(p) == 47547


def test_agi_values_truncate_like_the_spreadsheet(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    assert dict(payslip_agi_values(p)) == {"011": 69933, "012": 922, "001": 22386}
    totals = agi_totals([p])
    assert totals["ag_base"] == 70855
    assert totals["ag_fees"] == 22262          # FK487, truncated
    assert totals["accrued_ag_fees"] == 22263  # voucher, rounded
    assert totals["tax"] == 22386


def test_tax_free_allowance_is_paid_but_not_in_ag_base(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    assert seeded["310"].default_rate == 2.50 and seeded["310"].unit == "km"   # 25 kr/mil
    p.lines.append(PayslipLine(salary_type_id=seeded["310"].id, description="Bilersättning",
                               quantity=210, rate=2.50, amount=525, sort_order=9))
    db.session.commit()
    assert net_pay(p) == 47547 + 525
    assert payslip_agi_values(p)["050"] == 1
    assert agi_totals([p])["ag_base"] == 70855
    assert voucher_balanced(salary_voucher_rows(p))


def test_ag_base_follows_the_field_flag(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    AgiFieldCode.query.filter_by(code="012").first().ag_base = False
    db.session.commit()
    assert agi_totals([p])["ag_base"] == 69933


def test_payslip_totals_are_rounded_like_the_payout(db, seeded, staffan):
    s = payslip_summary(_june_slip(db, staffan, seeded))
    assert (s["cash_gross"], s["benefits"], s["tax"], s["net_pay"]) == (69933, 922, 22386, 47547)
    assert s["cash_gross"] - s["tax"] + s["tax_free"] - s["net_deduct"] == s["net_pay"]
    assert s["gross"] == 70855


def test_mileage_above_skatteverket_rate_is_flagged(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    p.lines.append(PayslipLine(salary_type_id=seeded["310"].id, description="Resa", quantity=100,
                               rate=2.50, amount=250, sort_order=9))
    db.session.commit()
    assert mileage_over_limit(p) == []
    p.lines[-1].rate = 3.00
    assert len(mileage_over_limit(p)) == 1


def test_tax_payment_goes_to_skattekonto(db):
    rows = tax_payment_rows(44648)
    assert [(r["account"], r["debit"], r["credit"]) for r in rows] == [("1630", 44648, 0), ("1930", 0, 44648)]


def test_tax_settlement_rounding_goes_to_3740(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    rows, diff = tax_settlement_rows([p], 22262 + 22386)
    assert diff == -1
    assert [(r["account"], r["debit"], r["credit"]) for r in rows] == [
        ("2710", 22386, 0), ("2731", 22263, 0), ("3740", 0, 1), ("1630", 0, 44648)]
    assert voucher_balanced(rows)


def test_tax_settlement_large_difference_stays_on_2731(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    rows, diff = tax_settlement_rows([p], 50000)
    assert diff > 10
    assert ("2731", 50000 - 22386, 0) in [(r["account"], r["debit"], r["credit"]) for r in rows]
    assert voucher_balanced(rows)


def test_agi_xml_structure_and_escaping(db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    p.status = "approved"
    fields = {f.code: f for f in AgiFieldCode.query.all()}
    xml = build_agi_xml("202606", [p], fields, {"org_nr": "559024-1047"},
                        {"name": "Edlund & Co", "phone": "070-1", "email": "a@b.se"})
    root = ET.fromstring(xml)
    assert root.get("omrade") == "Arbetsgivardeklaration"
    assert root.find(f"{AGD}Avsandare/{AGD}Organisationsnummer").text == "165590241047"
    assert root.find(f".//{AGD}TekniskKontaktperson/{AGD}Namn").text == "Edlund & Co"
    hu = root.find(f".//{AGD}HU")
    assert hu.find(f"{AGD}SummaArbAvgSlf").text == "22262"
    assert hu.find(f"{AGD}SummaSkatteavdr").text == "22386"
    iu = root.find(f".//{AGD}IU")
    assert iu.find(f".//{AGD}BetalningsmottagarId").text == "197901197512"
    assert iu.find(f"{AGD}Specifikationsnummer").text == "001"
    assert iu.find(f"{AGD}KontantErsattningUlagAG").text == "69933"
    assert iu.find(f"{AGD}SkatteplOvrigaFormanerUlagAG").text == "922"
    assert iu.find(f"{AGD}AvdrPrelSkatt").text == "22386"
    assert iu.find(f"{AGD}KontantErsattningUlagAG").get("faltkod") == "011"


def test_helpers():
    assert normalize_personnummer("790119-7512", today=date(2026, 10, 1)) == "197901197512"
    assert normalize_personnummer("050101-1234", today=date(2026, 10, 1)) == "200501011234"
    assert normalize_personnummer("197901197512") == "197901197512"
    with pytest.raises(ValueError):
        normalize_personnummer("123")
    assert add_month(date(2026, 1, 31)) == date(2026, 2, 28)
    assert next_period("2026-12") == "2027-01"
    assert skv_due_date("202606") == date(2026, 7, 12)
    assert skv_due_date("202607") == date(2026, 8, 17)
    assert skv_due_date("202612") == date(2027, 1, 17)


# ── Flow through the app ──────────────────────────────────────────────────────

def _future_payday(days=20):
    d = date.today() + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_edit_payslip_accepts_swedish_decimals(auth_client, db, seeded, staffan):
    p = Payslip(employee_id=staffan.id, period="2026-07", pay_date=date(2026, 7, 24), spec_nr=1)
    db.session.add(p)
    db.session.commit()
    r = auth_client.post(f"/payroll/payslips/{p.id}", data={
        "period": "2026-07", "pay_date": "2026-07-24", "note": "",
        "salary_type_id": [seeded["100"].id, seeded["110"].id, seeded["310"].id, ""],
        "description": ["Lön", "", "Resa", ""],
        "quantity": ["", "", "12,5", ""],
        "rate": ["", "", "25", ""],
        "amount": ["70 000", "66,58", "", ""],
    })
    assert r.status_code == 302
    db.session.refresh(p)
    amounts = [(l.salary_type.code, l.amount) for l in p.lines]
    assert amounts == [("100", 70000.0), ("110", 66.58), ("310", 312.5)]
    assert p.lines[1].description == "Bruttolöneavdrag"   # blank falls back to the löneart name


def test_copy_moves_one_month_and_keeps_lines(auth_client, db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    r = auth_client.post(f"/payroll/payslips/{p.id}/copy")
    assert r.status_code == 302
    copy = Payslip.query.filter(Payslip.id != p.id).one()
    assert copy.period == "2026-07"
    assert copy.pay_date == date(2026, 7, 25)
    assert copy.status == "draft"
    assert [(l.description, l.amount) for l in copy.lines] == [(l.description, l.amount) for l in p.lines]
    assert net_pay(copy) == net_pay(p)


def test_full_salary_and_tax_flow(auth_client, db, seeded, staffan, fortnox, app):
    app.config.update(COMPANY_ORG_NR="559024-1047", COMPANY_BBAN="65011234567", COMPANY_NAME="Test AB")
    for k, v in {"payroll_agi_contact_name": "Staffan", "payroll_agi_contact_phone": "070",
                 "payroll_agi_contact_email": "s@e.se"}.items():
        app_module.Settings.set(k, v)
    pay_date = _future_payday(20)
    p = _june_slip(db, staffan, seeded, pay_date=pay_date)

    # Register payment → locked and in the payment run
    auth_client.post(f"/payroll/payslips/{p.id}/approve")
    db.session.refresh(p)
    assert p.status == "approved"
    assert b"Edlund" in auth_client.get("/supplier-invoices/payment-run").data

    # Payment file: salary on its own pay date, SALA, to the employee's BBAN
    r = auth_client.post("/supplier-invoices/payment-run", data={"payslip_ids": [p.id]})
    assert r.status_code == 200
    root = ET.fromstring(r.data)
    blocks = root.findall(f".//{PAIN}PmtInf")
    assert len(blocks) == 1
    assert blocks[0].find(f"{PAIN}ReqdExctnDt").text == pay_date.isoformat()
    assert blocks[0].find(f".//{PAIN}CtgyPurp/{PAIN}Cd").text == "SALA"
    assert blocks[0].find(f".//{PAIN}InstdAmt").text == "47547.00"
    assert blocks[0].find(f".//{PAIN}CdtrAcct/{PAIN}Id/{PAIN}Othr/{PAIN}Id").text == "33007901197512"

    # Acknowledge → salary voucher on the pay date
    pf = PaymentFile.query.one()
    auth_client.post(f"/supplier-invoices/payment-files/{pf.id}/confirm")
    db.session.refresh(p)
    assert p.status == "paid"
    assert p.voucher_ref
    posted = fortnox.posted[-1]
    assert posted["date"] == pay_date
    assert posted["description"] == f"Lön Staffan {p.period}"
    assert posted["series"] == "A"

    # AGI: generate → acknowledge import → Skatteverket's amount → payment → voucher
    period = p.agi_period
    r = auth_client.post(f"/payroll/agi/{period}/xml")
    assert r.status_code == 200 and b"<agd:IU>" in r.data
    report = AgiReport.query.filter_by(period=period).one()
    assert report.status == "generated" and report.sum_ag_fees == 22262
    auth_client.post(f"/payroll/agi/{period}/submitted")
    skv_date = _future_payday(30)
    debit_date = skv_date + timedelta(days=2)
    auth_client.post(f"/payroll/agi/{period}/assess", data={
        "skv_amount": "44648", "skv_date": skv_date.isoformat(),
        "skv_debit_date": debit_date.isoformat(), "skv_ocr": "1655902410478"})
    db.session.refresh(report)
    assert report.status == "approved"

    r = auth_client.post("/supplier-invoices/payment-run", data={"agi_ids": [report.id]})
    root = ET.fromstring(r.data)
    block = root.find(f".//{PAIN}PmtInf")
    assert block.find(f"{PAIN}ReqdExctnDt").text == skv_date.isoformat()
    assert block.find(f".//{PAIN}CdtrAcct/{PAIN}Id/{PAIN}Othr/{PAIN}Id").text == "50501055"

    pf2 = PaymentFile.query.filter(PaymentFile.id != pf.id).one()
    auth_client.post(f"/supplier-invoices/payment-files/{pf2.id}/confirm")
    db.session.refresh(report)
    assert report.status == "paid" and report.voucher_ref and report.settle_voucher_ref
    payment, settlement = fortnox.posted[-2:]
    assert payment["date"] == skv_date
    assert [(r["account"], r["debit"], r["credit"]) for r in payment["rows"]] == [
        ("1630", 44648, 0), ("1930", 0, 44648)]
    assert settlement["date"] == debit_date
    assert [(r["account"], r["debit"], r["credit"]) for r in settlement["rows"]] == [
        ("2710", 22386, 0), ("2731", 22263, 0), ("3740", 0, 1), ("1630", 0, 44648)]


def test_settlement_retry_books_only_the_missing_voucher(auth_client, db, seeded, staffan, fortnox):
    p = _june_slip(db, staffan, seeded)
    p.status = "paid"
    r = AgiReport(period=p.agi_period, status="paid", skv_amount=44648, skv_date=date(2026, 7, 10),
                  skv_debit_date=date(2026, 7, 12), skv_ocr="1", voucher_ref="2026-A1")
    db.session.add(r)
    db.session.commit()
    auth_client.post(f"/payroll/agi/{p.agi_period}/book")
    db.session.refresh(r)
    assert r.voucher_ref == "2026-A1" and r.settle_voucher_ref
    assert len(fortnox.posted) == 1 and fortnox.posted[0]["date"] == date(2026, 7, 12)


def test_booking_files_payslip_in_fortnox_inbox_named_by_voucher(auth_client, db, seeded, staffan,
                                                                   fortnox, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "_email_pdf_to_fortnox_inbox",
                        lambda path, name, ref, config: sent.append((open(path, "rb").read(4), name, ref)))
    p = _june_slip(db, staffan, seeded)
    p.status = "paid"
    db.session.commit()
    auth_client.post(f"/payroll/payslips/{p.id}/book")
    db.session.refresh(p)
    assert sent == [(b"%PDF", f"{p.voucher_ref} - Lonebesked_Staffan_Edlund_2026-06.pdf", p.voucher_ref)]


def test_mixed_file_keeps_each_date(auth_client, db, seeded, staffan, fortnox, app):
    """Supplier invoices keep their due-date logic; the salary keeps its pay date."""
    app.config.update(COMPANY_BBAN="65011234567")
    pay_date = _future_payday(25)
    p = _june_slip(db, staffan, seeded, pay_date=pay_date)
    p.status = "approved"
    due = _future_payday(10)
    inv = SupplierInvoice(supplier_name="Lev AB", amount_incl_vat=1000, due_date=due,
                          payment_account="5050-1055", payment_account_type="bg",
                          payment_ref="1234", status="booked")
    db.session.add(inv)
    db.session.commit()
    r = auth_client.post("/supplier-invoices/payment-run",
                         data={"invoice_ids": [inv.id], "payslip_ids": [p.id]})
    root = ET.fromstring(r.data)
    dates = {b.find(f".//{PAIN}CtgyPurp/{PAIN}Cd").text: b.find(f"{PAIN}ReqdExctnDt").text
             for b in root.findall(f".//{PAIN}PmtInf")}
    assert dates["SALA"] == pay_date.isoformat()
    assert dates["SUPP"] < pay_date.isoformat()
    assert root.find(f".//{PAIN}GrpHdr/{PAIN}NbOfTxs").text == "2"

    pf = PaymentFile.query.one()
    auth_client.post(f"/supplier-invoices/payment-files/{pf.id}/confirm")
    supplier_post = [x for x in fortnox.posted if "supplier" in x][0]
    assert supplier_post["date"].isoformat() == dates["SUPP"]


def test_locked_payslip_cannot_be_edited(auth_client, db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    p.status = "approved"
    db.session.commit()
    auth_client.post(f"/payroll/payslips/{p.id}", data={
        "salary_type_id": [seeded["100"].id], "description": ["x"], "quantity": [""],
        "rate": [""], "amount": ["1"]})
    db.session.refresh(p)
    assert len(p.lines) == 5


def test_past_pay_date_is_refused_in_payment_run(auth_client, db, seeded, staffan):
    p = _june_slip(db, staffan, seeded, pay_date=date.today() - timedelta(days=3))
    p.status = "approved"
    db.session.commit()
    auth_client.post("/supplier-invoices/payment-run", data={"payslip_ids": [p.id]})
    assert PaymentFile.query.count() == 0


def test_payroll_pages_render(auth_client, db, seeded, staffan):
    p = _june_slip(db, staffan, seeded)
    for url in ["/payroll", "/payroll/employees", f"/payroll/employees/{staffan.id}",
                f"/payroll/payslips/{p.id}", f"/payroll/agi/{p.agi_period}",
                "/settings/payroll", "/settings/salary-types", "/settings/agi-codes",
                "/supplier-invoices/payment-run"]:
        assert auth_client.get(url).status_code == 200, url
    pdf = auth_client.get(f"/payroll/payslips/{p.id}/pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"


def test_historical_slip_counts_in_ytd_but_nowhere_else(auth_client, db, seeded, staffan):
    old = _june_slip(db, staffan, seeded, pay_date=date(2026, 5, 25))
    auth_client.post(f"/payroll/payslips/{old.id}/historical")
    db.session.refresh(old)
    assert old.status == "historical" and old.is_locked

    new = _june_slip(db, staffan, seeded)
    ytd = app_module._payslip_ytd(new)
    assert ytd["gross"] == 2 * payslip_summary(new)["gross"]
    assert ytd["tax"] == 2 * 22386

    assert app_module._payslips_in_agi_period(old.agi_period, include_drafts=True) == []
    assert auth_client.get("/payroll").status_code == 200
    assert auth_client.get(f"/payroll/payslips/{old.id}").status_code == 200

    auth_client.post(f"/payroll/payslips/{old.id}/historical")
    db.session.refresh(old)
    assert old.status == "draft"


# ── Preliminary tax from Skatteverket's table ─────────────────────────────────

# Table 34, 2026, as Skatteverket's open-data API returns it (a few brackets).
SKV_ROWS = [
    {"antal dgr": "30B", "år": "2026", "tabellnr": "34", "inkomst fr.o.m.": "36601", "inkomst t.o.m.": "36800",
     "kolumn 1": "7813", "kolumn 2": "7663", "kolumn 3": "4558", "kolumn 4": "7813", "kolumn 5": "11921",
     "kolumn 6": "11921", "kolumn 7": ""},
    {"antal dgr": "30B", "år": "2026", "tabellnr": "34", "inkomst fr.o.m.": "69801", "inkomst t.o.m.": "70000",
     "kolumn 1": "21846", "kolumn 2": "21769", "kolumn 3": "18665", "kolumn 4": "19360", "kolumn 5": "26273",
     "kolumn 6": "26273", "kolumn 7": ""},
    {"antal dgr": "30%", "år": "2026", "tabellnr": "34", "inkomst fr.o.m.": "1275601", "inkomst t.o.m.": "",
     "kolumn 1": "53", "kolumn 2": "53", "kolumn 3": "53", "kolumn 4": "45", "kolumn 5": "53",
     "kolumn 6": "53", "kolumn 7": ""},
    {"antal dgr": "14B", "år": "2026", "tabellnr": "34", "inkomst fr.o.m.": "1", "inkomst t.o.m.": "900",
     "kolumn 1": "1", "kolumn 2": "1", "kolumn 3": "1", "kolumn 4": "1", "kolumn 5": "1", "kolumn 6": "1"},
]


@pytest.fixture
def table34(db):
    from models import TaxTableRow
    from payroll import parse_skv_tax_rows
    db.session.add_all([TaxTableRow(year=2026, table_nr=34, **r) for r in parse_skv_tax_rows(SKV_ROWS)])
    db.session.commit()


def test_parse_skv_tax_rows_keeps_the_monthly_table():
    from payroll import parse_skv_tax_rows
    rows = parse_skv_tax_rows(SKV_ROWS)
    assert [(r["kind"], r["income_from"], r["income_to"], r["col1"]) for r in rows] == [
        ("B", 36601, 36800, 7813), ("B", 69801, 70000, 21846), ("%", 1275601, None, 53)]


def test_fetch_skv_tax_table_pages_until_a_short_page():
    from payroll import fetch_skv_tax_table
    calls = []

    class Resp:
        def __init__(self, results): self.results = results
        def raise_for_status(self): pass
        def json(self): return {"results": self.results}

    def get(url, params, timeout):
        calls.append(params["_offset"])
        return Resp([SKV_ROWS[0]] * 500 if params["_offset"] == 0 else SKV_ROWS[1:3])

    rows = fetch_skv_tax_table(2026, 34, get=get)
    assert calls == [0, 500] and len(rows) == 502


def _slip_with_absence(db, employee, types, tax=21846.0):
    p = Payslip(employee_id=employee.id, period="2026-11", pay_date=date(2026, 11, 25), spec_nr=1)
    p.lines = [PayslipLine(salary_type_id=types["100"].id, description="Månadslön", amount=70000.0, sort_order=0),
               PayslipLine(salary_type_id=types["900"].id, description="Preliminär skatt", amount=tax, sort_order=1)]
    db.session.add(p)
    db.session.commit()
    return p


def _post_lines(client, p, lines, action=None):
    data = {"period": p.period, "pay_date": p.pay_date.isoformat(), "note": "",
            "salary_type_id": [l[0] for l in lines], "description": [l[1] for l in lines],
            "quantity": [l[2] for l in lines], "rate": [l[3] for l in lines], "amount": [l[4] for l in lines]}
    if action:
        data["action"] = action
    return client.post(f"/payroll/payslips/{p.id}", data=data)


def test_recalc_tax_follows_the_gross_after_unpaid_leave(auth_client, db, seeded, staffan, table34):
    p = _slip_with_absence(db, staffan, seeded)
    r = _post_lines(auth_client, p, [
        (seeded["100"].id, "Månadslön", "", "", "70000"),
        (seeded["110"].id, "Tjänstledighet", "10", "3333,33", ""),
        (seeded["310"].id, "Resa", "100", "2,5", ""),          # tax-free: not part of the base
        (seeded["900"].id, "Preliminär skatt", "", "", "21846"),
    ], action="recalc_tax")
    assert r.status_code == 302
    db.session.refresh(p)
    by_code = {l.salary_type.code: l.amount for l in p.lines}
    assert by_code["110"] == 33333.3
    assert by_code["900"] == 7813.0                       # 70 000 − 33 333,30 = 36 666 → bracket 36 601–36 800
    assert payslip_summary(p)["tax"] == 7813


def test_recalc_tax_adds_the_tax_line_and_reads_percent_rows(auth_client, db, seeded, staffan, table34):
    p = _slip_with_absence(db, staffan, seeded)
    _post_lines(auth_client, p, [(seeded["100"].id, "Månadslön", "", "", "2000000")], action="recalc_tax")
    db.session.refresh(p)
    assert [(l.salary_type.code, l.amount) for l in p.lines] == [("100", 2000000.0), ("900", 1060000.0)]


def test_plain_save_leaves_the_tax_alone(auth_client, db, seeded, staffan, table34):
    p = _slip_with_absence(db, staffan, seeded)
    _post_lines(auth_client, p, [(seeded["100"].id, "Månadslön", "", "", "36700"),
                                 (seeded["900"].id, "Preliminär skatt", "", "", "21846")])
    db.session.refresh(p)
    assert p.lines[1].amount == 21846.0


@pytest.mark.parametrize("table, pay_date", [("", date(2026, 11, 25)), ("34", date(2027, 1, 25)),
                                               ("jämkning 25 %", date(2026, 11, 25))])
def test_recalc_tax_without_a_usable_table_saves_but_keeps_the_tax(auth_client, db, seeded, staffan,
                                                                   table34, table, pay_date):
    staffan.tax_table = table
    p = _slip_with_absence(db, staffan, seeded)
    p.pay_date = pay_date
    db.session.commit()
    _post_lines(auth_client, p, [(seeded["100"].id, "Månadslön", "", "", "36700"),
                                 (seeded["900"].id, "Preliminär skatt", "", "", "21846")], action="recalc_tax")
    db.session.refresh(p)
    assert [(l.salary_type.code, l.amount) for l in p.lines] == [("100", 36700.0), ("900", 21846.0)]


def test_tax_table_admin_fetch_view_delete(auth_client, db, seeded, staffan, monkeypatch):
    from models import TaxTableRow
    from payroll import parse_skv_tax_rows
    monkeypatch.setattr(app_module, "fetch_skv_tax_table", lambda year, nr: parse_skv_tax_rows(SKV_ROWS))
    r = auth_client.post("/settings/tax-tables", data={"action": "fetch", "year": "2026", "table_nr": "34"})
    assert r.status_code == 302
    auth_client.post("/settings/tax-tables", data={"action": "fetch", "year": "2026", "table_nr": "34"})
    assert TaxTableRow.query.filter_by(year=2026, table_nr=34).count() == 3      # refetch replaces
    page = auth_client.get("/settings/tax-tables?year=2026&table_nr=34&income=70000")
    assert page.status_code == 200 and "21 846" in page.get_data(as_text=True)
    assert auth_client.get("/settings/tax-tables").status_code == 200
    auth_client.post("/settings/tax-tables", data={"action": "delete", "year": "2026", "table_nr": "34"})
    assert TaxTableRow.query.count() == 0


def test_tax_table_fetch_failure_is_reported(auth_client, db, monkeypatch):
    def boom(year, nr):
        raise RuntimeError("offline")
    monkeypatch.setattr(app_module, "fetch_skv_tax_table", boom)
    r = auth_client.post("/settings/tax-tables", data={"action": "fetch", "year": "2026", "table_nr": "34"},
                         follow_redirects=True)
    assert r.status_code == 200 and "Skatteverket" in r.get_data(as_text=True)
