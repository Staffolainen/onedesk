"""
Payroll (lönebesked) — the pure calculations behind payslips, the AGI file and
the two vouchers. No Flask, no database session: everything here takes model
objects (or anything shaped like them) and returns plain data, so the payslip
page, the PDF, the Fortnox posting and the tests all read from one source.

Rounding follows how the books are actually kept:
  * every reported and booked amount is whole kronor, truncated toward zero —
    the same TRUNC the AGI uses, so 2710/7220 always agree with FK001/FK011;
  * net pay and every total on the payslip are derived from those truncated rows,
    so the slip, the payout and the voucher show the same kronor;
  * arbetsgivaravgift on the voucher is rounded half up (70 855 × 31,42 % = 22 263),
    while FK487 in the AGI is truncated (22 262) as Skatteverket computes it. The
    öre-level difference is cleared when the tax payment is booked.
"""
from collections import OrderedDict
from datetime import date
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from xml.sax.saxutils import escape
import calendar
import re

AGI_NS_INSTANS   = "http://xmls.skatteverket.se/se/skatteverket/da/instans/schema/1.1"
AGI_NS_KOMPONENT = "http://xmls.skatteverket.se/se/skatteverket/da/komponent/schema/1.1"
SKV_BANKGIRO     = "5050-1055"

# kind → (sign on the slip, taxable/AG-base, part of net pay, Swedish label, English label)
KINDS = OrderedDict([
    ("cash",            ( 1, True,  True,  "Kontant lön",          "Cash pay")),
    ("gross_deduction", (-1, True,  True,  "Bruttolöneavdrag",     "Gross deduction")),
    ("benefit",         ( 1, True,  False, "Förmån",               "Taxable benefit")),
    ("tax_free",        ( 1, False, True,  "Skattefri ersättning", "Tax-free allowance")),
    ("tax",             (-1, False, True,  "Preliminärskatt",      "Preliminary tax")),
    ("net_deduction",   (-1, False, True,  "Nettolöneavdrag",      "Net deduction")),
])

# Default accounts; overridable on the payroll settings page.
DEFAULT_ACCOUNTS = {
    "bank":            "1930",
    "ag_cost":         "7510",
    "ag_liability":    "2731",
    "tax_liability":   "2710",
    "tax_payment":     "1930",   # bank the Skatteverket payment leaves from
    "tax_account":     "1630",   # skattekonto
    "rounding":        "3740",
}
ROUNDING_TOLERANCE = 10   # kr; a larger gap on the tax settlement stays on 2731 and is flagged

# Skatteverket's tax-free bilersättning, own car: 25 kr/mil = 2,50 kr/km (2023 →, unchanged 2026).
# Anything paid above it is taxable pay and belongs on a cash löneart (FK011).
SKV_MILEAGE_RATE_PER_KM = 2.50

# Skatteverket AGI fields, schema 1.1 (teknisk beskrivning 1.1.18).
# (code, XML element, value type, description)
AGI_FIELD_SEED = [
    ("001", "AvdrPrelSkatt", "amount", 'Avdragen preliminär skatt'),
    ("004", "ErsattningsKod1", "text", 'Ersättningskod 1 (pension/försäkring)'),
    ("005", "ErsattningsBelopp1", "amount", 'Ersättningsbelopp 1'),
    ("006", "RedovisningsPeriod", "text", 'Redovisningsperiod (ÅÅÅÅMM)'),
    ("010", "AvrakningAvgiftsfriErs", "amount", 'Avräkning avgiftsfri ersättning'),
    ("011", "KontantErsattningUlagAG", "amount", 'Kontant ersättning, underlag arbetsgivaravgifter (bruttolön)'),
    ("012", "SkatteplOvrigaFormanerUlagAG", "amount", 'Övriga skattepliktiga förmåner (t.ex. cykelförmån)'),
    ("013", "SkatteplBilformanUlagAG", "amount", 'Skattepliktig bilförmån utom drivmedel'),
    ("018", "DrivmVidBilformanUlagAG", "amount", 'Drivmedel vid bilförmån (marknadsvärde × 1,2)'),
    ("019", "AvdragUtgiftArbetet", "amount", 'Avdrag för utgifter i arbetet'),
    ("020", "AndraKostnadsers", "amount", 'Andra kostnadsersättningar'),
    ("021", "UnderlagRutarbete", "amount", 'Underlag rutarbete'),
    ("022", "UnderlagRotarbete", "amount", 'Underlag rotarbete'),
    ("023", "AndelStyrelsearvode", "amount", 'Andel styrelsearvode'),
    ("026", "Fartygssignal", "text", 'Fartygssignal'),
    ("027", "AntalDagarSjoinkomst", "text", 'Antal dagar med sjöinkomst'),
    ("028", "NarfartFjarrfart", "text", 'NarfartFjarrfart'),
    ("030", "Tjanstepension", "amount", 'Tjänstepension'),
    ("032", "ErsEjSocAvgEjJobbavd", "amount", 'Ersättning som inte är underlag för soc.avg. och inte jobbskatteavdrag'),
    ("035", "Forskarskattenamnden", "amount", 'Beslut från Forskarskattenämnden'),
    ("036", "ErsFormanBostadMmSINK", "amount", 'Ersättning/förmån bostad m.m. SINK'),
    ("037", "VissaAvdrag", "amount", 'Vissa avdrag'),
    ("039", "Hyresersattning", "amount", 'Hyresersättning'),
    ("041", "BostadsformanSmahusUlagAG", "checkbox", 'Bostadsförmån småhus (kryss)'),
    ("043", "BostadsformanEjSmahusUlagAG", "checkbox", 'Bostadsförmån ej småhus (kryss)'),
    ("048", "FormanHarJusterats", "checkbox", 'Förmån har justerats (kryss)'),
    ("050", "Bilersattning", "checkbox", 'Bilersättning (kryss)'),
    ("051", "Traktamente", "checkbox", 'Traktamente (kryss)'),
    ("059", "Personaloption", "checkbox", 'Personaloption (kryss)'),
    ("060", "Arbetsstallenummer", "text", 'Arbetsställenummer'),
    ("062", "ForstaAnstalld", "checkbox", 'Första anställd (kryss)'),
    ("063", "VaxaStod", "checkbox", 'Växa-stöd (kryss) – utgår fr.o.m. jan 2026'),
    ("076", "LandskodTIN", "text", 'Landskod TIN'),
    ("077", "Fodelseort", "text", 'Födelseort'),
    ("078", "LandskodFodelseort", "text", 'Landskod födelseort'),
    ("081", "LandskodMedborgare", "text", 'Landskod medborgarskap'),
    ("084", "ErsattningsKod2", "text", 'ErsattningsKod2'),
    ("085", "ErsattningsBelopp2", "amount", 'ErsattningsBelopp2'),
    ("086", "ErsattningsKod3", "text", 'ErsattningsKod3'),
    ("087", "ErsattningsBelopp3", "amount", 'ErsattningsBelopp3'),
    ("088", "ErsattningsKod4", "text", 'ErsattningsKod4'),
    ("089", "ErsattningsBelopp4", "amount", 'ErsattningsBelopp4'),
    ("090", "LandskodArbetsland", "text", 'Landskod arbetsland'),
    ("091", "UtsandUnderTid", "text", 'Utsänd under tid'),
    ("094", "AmbassadanstISvMAvtal", "checkbox", 'AmbassadanstISvMAvtal'),
    ("098", "BetForDrivmVidBilformanUlagAG", "amount", 'Betalning för drivmedel vid bilförmån'),
    ("112", "VerksamhetensArt", "text", 'Verksamhetens art'),
    ("114", "SkattebefrEnlAvtal", "checkbox", 'Skattebefriad enligt avtal (kryss)'),
    ("123", "BostadsformanSmahusUlagEA", "checkbox", 'BostadsformanSmahusUlagEA'),
    ("124", "BostadsformanEjSmahusUlagEA", "checkbox", 'BostadsformanEjSmahusUlagEA'),
    ("125", "KontantErsattningUlagEA", "amount", 'KontantErsattningUlagEA'),
    ("126", "SkatteplOvrigaFormanerUlagEA", "amount", 'SkatteplOvrigaFormanerUlagEA'),
    ("127", "SkatteplBilformanUlagEA", "amount", 'SkatteplBilformanUlagEA'),
    ("128", "DrivmVidBilformanUlagEA", "amount", 'DrivmVidBilformanUlagEA'),
    ("131", "KontantErsattningEjUlagSA", "amount", 'KontantErsattningEjUlagSA'),
    ("132", "SkatteplOvrigaFormanerEjUlagSA", "amount", 'SkatteplOvrigaFormanerEjUlagSA'),
    ("133", "SkatteplBilformanEjUlagSA", "amount", 'SkatteplBilformanEjUlagSA'),
    ("134", "DrivmVidBilformanEjUlagSA", "amount", 'DrivmVidBilformanEjUlagSA'),
    ("136", "FormanSomPensionEjUlagSA", "checkbox", 'FormanSomPensionEjUlagSA'),
    ("137", "BostadsformanSmahusEjUlagSA", "checkbox", 'BostadsformanSmahusEjUlagSA'),
    ("138", "BostadsformanEjSmahusEjUlagSA", "checkbox", 'BostadsformanEjSmahusEjUlagSA'),
    ("201", "AgRegistreradId", "text", 'Arbetsgivarens org.-/personnummer, 12 siffror'),
    ("205", "Borttag", "checkbox", 'Borttag av tidigare inlämnad uppgift (kryss)'),
    ("215", "BetalningsmottagarId", "text", 'Betalningsmottagarens person-/samordningsnummer, 12 siffror'),
    ("216", "Fornamn", "text", 'Förnamn'),
    ("217", "Efternamn", "text", 'Efternamn'),
    ("218", "Gatuadress", "text", 'Gatuadress'),
    ("219", "Postnummer", "text", 'Postnummer'),
    ("220", "Postort", "text", 'Postort'),
    ("221", "LandskodPostort", "text", 'Landskod postort'),
    ("222", "Fodelsetid", "text", 'Födelsetid'),
    ("223", "FartygetsNamn", "text", 'FartygetsNamn'),
    ("224", "AnnatId", "text", 'Annat ID'),
    ("226", "OrgNamn", "text", 'Organisationsnamn'),
    ("228", "Gatuadress2", "text", 'Gatuadress 2'),
    ("230", "FriAdress", "text", 'Fri adress'),
    ("245", "ArbetsplatsensGatuadress", "text", 'Arbetsplatsens gatuadress'),
    ("246", "ArbetsplatsensOrt", "text", 'Arbetsplatsens ort'),
    ("252", "TIN", "text", 'TIN (utländskt skatteregistreringsnr)'),
    ("253", "Lokalanstalld", "checkbox", 'Lokalanställd (kryss)'),
    ("274", "AvdrSkattSINK", "amount", 'Avdragen skatt SINK'),
    ("275", "AvdrSkattASINK", "amount", 'Avdragen skatt A-SINK'),
    ("276", "EjskatteavdragEjbeskattningSv", "checkbox", 'Ej skatteavdrag/ej beskattning i Sverige (kryss)'),
    ("302", "EjFastDriftstalleISv", "checkbox", 'Ej fast driftställe i Sverige (kryss)'),
    ("303", "EjFastDriftstalleIndivid", "checkbox", 'Ej fast driftställe, individnivå (kryss)'),
    ("305", "KonventionMed", "text", 'Konvention med land'),
    ("463", "AnnatDriftstod", "amount", 'Annat driftstöd'),
    ("470", "UlagFoU", "amount", 'Underlag FoU-avdrag'),
    ("471", "UlagRegionaltStod", "amount", 'Underlag regionalt stöd'),
    ("472", "UlagRederiRegress", "amount", 'Underlag rederistöd'),
    ("475", "AvdragFoU", "amount", 'Avdrag FoU'),
    ("476", "AvdragRegionaltStod", "amount", 'Avdrag regionalt stöd'),
    ("477", "AvdragRederiRegress", "amount", 'Avdrag rederistöd'),
    ("481", "UlagSlfVinstandel", "amount", 'Underlag SLF vinstandel'),
    ("486", "SlfVinstandelFK486", "amount", 'SLF vinstandel'),
    ("487", "SummaArbAvgSlf", "amount", 'Summa arbetsgivaravgifter och SLF'),
    ("491", "UlagSkatteavdrPensFors", "amount", 'Underlag skatteavdrag pensionsförsäkring'),
    ("492", "UlagSkatteavdrRantaUtd", "amount", 'Underlag skatteavdrag ränta/utdelning'),
    ("495", "SkatteavdrPensFors", "amount", 'Skatteavdrag pensionsförsäkring'),
    ("496", "SkatteavdrRantaUtd", "amount", 'Skatteavdrag ränta/utdelning'),
    ("497", "SummaSkatteavdr", "amount", 'Summa avdragen skatt'),
    ("499", "TotalSjuklonekostnad", "amount", 'Total sjuklönekostnad'),
    ("570", "Specifikationsnummer", "text", 'Specifikationsnummer (unikt per mottagare och period)'),
    ("820", "FranvaroBorttag", "checkbox", 'Frånvaro: borttag (kryss)'),
    ("821", "FranvaroDatum", "text", 'Frånvaro: datum'),
    ("822", "FranvaroSpecifikationsnummer", "text", 'Frånvaro: specifikationsnummer'),
    ("823", "FranvaroTyp", "text", 'Frånvaro: typ'),
    ("824", "FranvaroProcentTFP", "text", 'Frånvaro: procent TFP'),
    ("825", "FranvaroTimmarTFP", "text", 'Frånvaro: timmar TFP'),
    ("826", "FranvaroProcentFP", "text", 'Frånvaro: procent FP'),
    ("827", "FranvaroTimmarFP", "text", 'Frånvaro: timmar FP'),
]
# Fields seeded as arbetsgivaravgift base (editable per field afterwards).
AGI_AG_BASE_CODES = {"011", "012", "013", "018"}
# Written by the generator itself, or belonging to HU / frånvaro — never on a löneart.
_STRUCTURAL = {"006", "201", "205", "215", "570"}


def agi_field_selectable(code, value_type):
    if code in _STRUCTURAL or value_type not in ("amount", "checkbox"):
        return False
    n = int(code)
    return n < 400   # 4xx are HU-level sums and deductions, 8xx frånvaro


# (code, name, kind, agi code, debit, credit, rate, unit)
SALARY_TYPE_SEED = [
    ("100", "Månadslön",                 "cash",            "011", "",     "",     None, ""),
    ("110", "Bruttolöneavdrag",          "gross_deduction", "011", "",     "",     None, ""),
    ("200", "Cykelförmån",               "benefit",         "012", "7330", "7399", None, ""),
    ("210", "Bilförmån",                 "benefit",         "013", "7385", "7399", None, ""),
    ("300", "Traktamente Sverige",       "tax_free",        "051", "7321", "",     None, "dagar"),
    ("301", "Traktamente utland",        "tax_free",        "051", "7323", "",     None, "dagar"),
    ("310", "Bilersättning egen bil",    "tax_free",        "050", "7331", "",     SKV_MILEAGE_RATE_PER_KM, "km"),
    ("900", "Preliminär skatt",          "tax",             "001", "",     "2710", None, ""),
]


# ── Small helpers ─────────────────────────────────────────────────────────────

def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def trunc_kr(value) -> int:
    """Whole kronor, truncated toward zero (Excel TRUNC)."""
    return int(_d(value).quantize(Decimal("1"), rounding=ROUND_DOWN))


def round_kr(value) -> int:
    return int(_d(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kind_sign(kind):
    return KINDS.get(kind, KINDS["cash"])[0]


def kind_label(kind, lang="sv"):
    k = KINDS.get(kind)
    return (k[3] if lang == "sv" else k[4]) if k else kind


def digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_personnummer(value, today=None) -> str:
    """Return the 12-digit form (ÅÅÅÅMMDDNNNN) of a 10- or 12-digit personnummer."""
    p = digits(value)
    if len(p) == 12:
        return p
    if len(p) != 10:
        raise ValueError("Personnummer måste ha 10 eller 12 siffror")
    today = today or date.today()
    yy = int(p[:2])
    century = 20 if yy <= today.year % 100 else 19
    # A "+" separator means 100 years or older.
    if "+" in str(value):
        century -= 1
    return f"{century}{p}"


def org_nr_12(org_nr) -> str:
    """Organisationsnummer as AGI wants it: 16 + 10 digits."""
    d = digits(org_nr)
    return d if len(d) == 12 else f"16{d}"


def add_month(d: date, months=1) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return add_month(date(y, m, 1)).strftime("%Y-%m")


def skv_due_date(agi_period: str) -> date:
    """Deklarations- och betalningsdag for an AGI period ("YYYYMM").

    The 12th of the following month — the 17th when that month is January or
    August. (Larger employers file earlier; this is the default only.)
    """
    d = add_month(date(int(agi_period[:4]), int(agi_period[4:6]), 1))
    return d.replace(day=17 if d.month in (1, 8) else 12)


def line_amount(quantity, rate, amount):
    """Quantity × rate when both are given, otherwise the amount as entered."""
    if quantity not in (None, "") and rate not in (None, ""):
        return float((_d(quantity) * _d(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return float(_d(amount))


# ── Payslip calculation ───────────────────────────────────────────────────────

def signed(line) -> Decimal:
    return _d(line.amount) * kind_sign(line.salary_type.kind)


def payslip_summary(payslip) -> dict:
    """Everything the slip shows, from its lines.

    gross        taxable pay incl. benefits (the AG base)
    cash_gross   cash pay after gross deductions
    benefits     taxable benefits (not paid out)
    tax_free     tax-free allowances
    tax          preliminary tax withheld (positive)
    net_deduct   net deductions (positive)
    net_pay      what lands on the employee's account
    ag_fee       arbetsgivaravgift for the voucher
    """
    rows = salary_voucher_rows(payslip)
    agi  = payslip_agi_values(payslip)
    raw = _salary_rows_raw(payslip)
    side = lambda role: sum(d - c for _a, d, c, _i, r in raw if r == role)
    cash_gross = side("salary")
    benefits = sum(trunc_kr(signed(l)) for l in payslip.lines if l.salary_type.kind == "benefit")
    return {
        "cash_gross": cash_gross,
        "benefits":   benefits,
        "tax_free":   side("allowance"),
        "tax":        -side("tax"),
        "net_deduct": -side("deduction"),
        "gross":      cash_gross + benefits,
        "ag_base":    ag_base(payslip),
        "ag_fee":     ag_fee(payslip),
        "net_pay":    net_pay(payslip),
        "agi":        agi,
        "voucher":    rows,
    }


def payslip_agi_values(payslip) -> "OrderedDict[str, int]":
    """AGI value per fältkod for this payslip.

    Amount fields are the signed sum of their lines, truncated (tax as a positive
    number); checkbox fields are 1 when any line carries an amount.
    """
    sums, boxes = OrderedDict(), set()
    for line in payslip.lines:
        st = line.salary_type
        field = st.agi_field
        if not field:
            continue
        if field.value_type == "checkbox":
            if _d(line.amount) != 0:
                boxes.add(field.code)
            continue
        value = signed(line)
        if st.kind == "tax":
            value = -value
        sums[field.code] = sums.get(field.code, Decimal(0)) + value
    out = OrderedDict((code, trunc_kr(v)) for code, v in sums.items())
    for code in sorted(boxes):
        out[code] = 1
    return out


def ag_base(payslip) -> int:
    """Sum of the AGI amounts whose field is flagged as arbetsgivaravgift base."""
    agi = payslip_agi_values(payslip)
    codes = {l.salary_type.agi_field.code for l in payslip.lines
             if l.salary_type.agi_field is not None and getattr(l.salary_type.agi_field, "ag_base", False)}
    return sum(agi.get(c, 0) for c in codes)


def ag_fee(payslip) -> int:
    return round_kr(_d(ag_base(payslip)) * _d(payslip.employee.ag_rate or 0))


def net_pay(payslip) -> int:
    """Net pay from the truncated voucher amounts, so the voucher balances."""
    total = 0
    for account, debit, credit, _info, role in _salary_rows_raw(payslip):
        if role in ("salary", "allowance"):
            total += debit - credit
        elif role in ("tax", "deduction"):
            total -= credit - debit
    return total


def _salary_rows_raw(payslip):
    """(account, debit, credit, info, role) rows before the bank and AG rows."""
    emp = payslip.employee
    pooled = OrderedDict()   # (account, role) → Decimal, signed toward debit
    benefit_rows = []
    for line in payslip.lines:
        st = line.salary_type
        amount = signed(line)
        if st.kind in ("cash", "gross_deduction"):
            key = (st.debit_account or emp.salary_account or "7210", "salary")
            pooled[key] = pooled.get(key, Decimal(0)) + amount
        elif st.kind == "tax_free":
            key = (st.debit_account or "7321", "allowance")
            pooled[key] = pooled.get(key, Decimal(0)) + amount
        elif st.kind == "tax":
            key = (st.credit_account or DEFAULT_ACCOUNTS["tax_liability"], "tax")
            pooled[key] = pooled.get(key, Decimal(0)) + amount
        elif st.kind == "net_deduction":
            key = (st.credit_account or "2790", "deduction")
            pooled[key] = pooled.get(key, Decimal(0)) + amount
        elif st.kind == "benefit":
            v = trunc_kr(amount)
            info = (line.description or st.name)[:100]
            if v:
                benefit_rows.append((st.debit_account or "7389", max(v, 0), max(-v, 0), info, "benefit"))
                benefit_rows.append((st.credit_account or "7399", max(-v, 0), max(v, 0), info, "benefit"))
    rows = []
    for (account, role), value in pooled.items():
        v = trunc_kr(value)
        if v:
            rows.append((account, max(v, 0), max(-v, 0), "", role))
    return rows + benefit_rows


def salary_voucher_rows(payslip, accounts=None) -> list:
    """The lönevoucher, in the order Fortnox shows it for a hand-booked salary.

    Bank (net pay) · salary · tax · benefits · arbetsgivaravgift.
    """
    acc = {**DEFAULT_ACCOUNTS, **(accounts or {})}
    raw = _salary_rows_raw(payslip)
    rows = []
    net = net_pay(payslip)
    if net:
        rows.append(_vrow(acc["bank"], max(-net, 0), max(net, 0), ""))
    for role in ("salary", "allowance", "tax", "deduction", "benefit"):
        for account, debit, credit, info, r in raw:
            if r == role:
                rows.append(_vrow(account, debit, credit, info))
    fee = ag_fee(payslip)
    if fee:
        pct = f"{(payslip.employee.ag_rate or 0) * 100:.2f}".replace(".", ",")
        rows.append(_vrow(acc["ag_cost"], fee, 0, f"Arbetsgivaravgifter {pct} %"))
        rows.append(_vrow(acc["ag_liability"], 0, fee, f"Arbetsgivaravgifter {pct} %"))
    return rows


def _vrow(account, debit, credit, info):
    return {"account": str(account), "debit": float(debit), "credit": float(credit), "info": info}


def voucher_balanced(rows) -> bool:
    return round(sum(r["debit"] for r in rows) - sum(r["credit"] for r in rows), 2) == 0


def salary_voucher_description(payslip) -> str:
    return f"Lön {payslip.employee.first_name} {payslip.period}"


# ── AGI ───────────────────────────────────────────────────────────────────────

def agi_totals(payslips) -> dict:
    """HU sums: FK487 (truncated, as Skatteverket computes it) and FK497."""
    base_by_rate = {}
    tax = 0
    accrued_fee = 0
    for p in payslips:
        rate = p.employee.ag_rate or 0
        base_by_rate[rate] = base_by_rate.get(rate, 0) + ag_base(p)
        tax += payslip_agi_values(p).get("001", 0)
        accrued_fee += ag_fee(p)
    fee = sum(trunc_kr(_d(base) * _d(rate)) for rate, base in base_by_rate.items())
    return {"ag_base": sum(base_by_rate.values()), "ag_fees": fee, "tax": tax,
            "total": fee + tax, "accrued_ag_fees": accrued_fee}


def _iu_field_order(code, value_type):
    """Amounts first, then the tax fields, then checkboxes — as in Skatteverket's examples."""
    if value_type == "checkbox":
        return (2, code)
    if code in ("001", "274", "275"):
        return (1, code)
    return (0, code)


def build_agi_xml(period, payslips, fields_by_code, company, contact, created=None) -> bytes:
    """Arbetsgivardeklaration, schema 1.1: one HU plus one IU per payslip.

    period          "YYYYMM"
    fields_by_code  {"011": AgiFieldCode, ...} — element names come from here
    company         {"name", "org_nr"}
    contact         {"name", "phone", "email"}
    """
    from datetime import datetime
    created = created or datetime.now()
    org = org_nr_12(company["org_nr"])
    e = lambda v: escape(str(v if v is not None else ""))
    totals = agi_totals(payslips)

    contact_xml = (f"<agd:Namn>{e(contact['name'])}</agd:Namn>"
                   f"<agd:Telefon>{e(contact['phone'])}</agd:Telefon>"
                   f"<agd:Epostadress>{e(contact['email'])}</agd:Epostadress>")
    head = (f"<agd:Arendeinformation><agd:Arendeagare>{org}</agd:Arendeagare>"
            f"<agd:Period>{period}</agd:Period></agd:Arendeinformation>")

    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        f'<Skatteverket omrade="Arbetsgivardeklaration" xmlns="{AGI_NS_INSTANS}" xmlns:agd="{AGI_NS_KOMPONENT}">',
        "  <agd:Avsandare>",
        f"    <agd:Programnamn>{e(company.get('program', 'onedesk'))}</agd:Programnamn>",
        f"    <agd:Organisationsnummer>{org}</agd:Organisationsnummer>",
        f"    <agd:TekniskKontaktperson>{contact_xml}</agd:TekniskKontaktperson>",
        f"    <agd:Skapad>{created.strftime('%Y-%m-%dT%H:%M:%S')}</agd:Skapad>",
        "  </agd:Avsandare>",
        "  <agd:Blankettgemensamt><agd:Arbetsgivare>",
        f"    <agd:AgRegistreradId>{org}</agd:AgRegistreradId>",
        f"    <agd:Kontaktperson>{contact_xml}</agd:Kontaktperson>",
        "  </agd:Arbetsgivare></agd:Blankettgemensamt>",
        f"  <agd:Blankett>{head}<agd:Blankettinnehall><agd:HU>",
        f'    <agd:ArbetsgivareHUGROUP><agd:AgRegistreradId faltkod="201">{org}</agd:AgRegistreradId></agd:ArbetsgivareHUGROUP>',
        f'    <agd:RedovisningsPeriod faltkod="006">{period}</agd:RedovisningsPeriod>',
        f'    <agd:SummaArbAvgSlf faltkod="487">{totals["ag_fees"]}</agd:SummaArbAvgSlf>',
        f'    <agd:SummaSkatteavdr faltkod="497">{totals["tax"]}</agd:SummaSkatteavdr>',
        "  </agd:HU></agd:Blankettinnehall></agd:Blankett>",
    ]
    for p in payslips:
        values = payslip_agi_values(p)
        fields = []
        for code, value in values.items():
            f = fields_by_code.get(code)
            if f is None:
                raise ValueError(f"Okänd fältkod {code} på lönebesked {p.id}")
            if f.value_type == "amount" and value == 0 and code != "001":
                continue
            fields.append((_iu_field_order(code, f.value_type), f.element, code, value))
        fields.sort()
        out += [
            f"  <agd:Blankett>{head}<agd:Blankettinnehall><agd:IU>",
            f'    <agd:ArbetsgivareIUGROUP><agd:AgRegistreradId faltkod="201">{org}</agd:AgRegistreradId></agd:ArbetsgivareIUGROUP>',
            "    <agd:BetalningsmottagareIUGROUP><agd:BetalningsmottagareIDChoice>"
            f'<agd:BetalningsmottagarId faltkod="215">{e(p.employee.personnummer)}</agd:BetalningsmottagarId>'
            "</agd:BetalningsmottagareIDChoice></agd:BetalningsmottagareIUGROUP>",
            f'    <agd:RedovisningsPeriod faltkod="006">{period}</agd:RedovisningsPeriod>',
            f'    <agd:Specifikationsnummer faltkod="570">{int(p.spec_nr or 1):03d}</agd:Specifikationsnummer>',
        ]
        out += [f'    <agd:{el} faltkod="{code}">{value}</agd:{el}>' for _o, el, code, value in fields]
        out.append("  </agd:IU></agd:Blankettinnehall></agd:Blankett>")
    out.append("</Skatteverket>")
    return ("\n".join(out) + "\n").encode("utf-8")


# ── Tax payment ───────────────────────────────────────────────────────────────

def tax_payment_rows(skv_amount, accounts=None) -> list:
    """The payment itself: bank → skattekonto, dated on the payment date."""
    acc = {**DEFAULT_ACCOUNTS, **(accounts or {})}
    amount = round(float(skv_amount or 0), 2)
    return [_vrow(acc["tax_account"], amount, 0, "Inbetalning skattekonto"),
            _vrow(acc["tax_payment"], 0, amount, "Skatteverket")]


def tax_settlement_rows(payslips, skv_amount, accounts=None) -> tuple:
    """Skatteverket's debit of the skattekonto on the tax date: clear 2710 and 2731 against 1630.

    Returns (rows, difference). The difference is SKV's amount minus what the
    salary vouchers accrued; within ROUNDING_TOLERANCE it goes to öresavrundning,
    beyond that it stays on 2731 (and the caller flags it).
    """
    acc = {**DEFAULT_ACCOUNTS, **(accounts or {})}
    tax = sum(payslip_agi_values(p).get("001", 0) for p in payslips)
    accrued = sum(ag_fee(p) for p in payslips)
    amount = round(float(skv_amount or 0), 2)
    diff = round(amount - tax - accrued, 2)
    rows = []
    if tax:
        rows.append(_vrow(acc["tax_liability"], tax, 0, "Personalskatt"))
    if abs(diff) <= ROUNDING_TOLERANCE:
        if accrued:
            rows.append(_vrow(acc["ag_liability"], accrued, 0, "Arbetsgivaravgifter"))
        if diff:
            rows.append(_vrow(acc["rounding"], max(diff, 0), max(-diff, 0), "Avrundning skattekonto"))
    else:
        rows.append(_vrow(acc["ag_liability"], max(accrued + diff, 0), max(-(accrued + diff), 0),
                          "Arbetsgivaravgifter"))
    rows.append(_vrow(acc["tax_account"], 0, amount, "Skattekonto"))
    return rows, diff


def mileage_over_limit(payslip) -> list:
    """Bilersättning lines (FK050) paid above Skatteverket's tax-free rate per km."""
    out = []
    for line in payslip.lines:
        f = line.salary_type.agi_field
        if f is not None and f.code == "050" and line.rate and line.quantity:
            unit = (line.salary_type.unit or "km").lower()
            per_km = float(line.rate) / 10 if unit == "mil" else float(line.rate)
            if per_km > SKV_MILEAGE_RATE_PER_KM + 1e-9:
                out.append(line)
    return out
