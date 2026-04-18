"""
ISO 20022 pain.001.001.03 payment file generator for Handelsbanken.

Used for supplier payments via "Betalningar/Löner [pain001]" in HB's internet bank.

Structure follows Handelsbanken's pain.001 specification (document 72-271582).
BG/PG and IBAN payments are placed in separate PmtInf blocks.

Config required:
    COMPANY_NAME        — debtor name
    COMPANY_BBAN        — debtor account clearing+account digits (e.g. 6501929223888)
    COMPANY_IBAN        — fallback if COMPANY_BBAN unset; BBAN derived from SE-IBAN
    COMPANY_BIC         — debtor bank BIC (HANDSESS for Handelsbanken)
    COMPANY_ORG_NR      — used as message ID prefix
"""
import uuid
import re as _re
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
import xml.etree.ElementTree as ET

NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
ET.register_namespace("", NS)


def _t(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, _t(tag))
    if text is not None:
        el.text = text
    return el


def _digits_only(value: str) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


def _fmt_amount(amount: float) -> str:
    return f"{amount:.2f}"


def _valid_ocr(ref: str) -> bool:
    """
    Validate a Swedish OCR reference using the Luhn (modulo-10) algorithm.
    Returns True if the last digit is the correct Luhn check digit.
    """
    digits = _digits_only(ref)
    if len(digits) < 2:
        return False
    total = 0
    double = False
    for d in reversed(digits):
        n = int(d)
        if double:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        double = not double
    return total % 10 == 0


def _clean_xml(raw: str) -> bytes:
    raw = _re.sub(r"\n[ \t]*\n", "\n", raw)
    return raw.encode("utf-8")


# ── Swedish bank holiday calendar ────────────────────────────────────────────

def _easter(year: int) -> _date:
    """Computus — returns Easter Sunday for a given year."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(114 + h + l - 7 * m, 31)
    return _date(year, month, day + 1)


def _swedish_bank_holidays(year: int) -> set:
    """
    Returns the set of Swedish bank holidays (röda dagar + bank-closed days)
    for the given year.
    """
    e = _easter(year)
    holidays = {
        _date(year, 1, 1),   # Nyårsdagen
        _date(year, 1, 6),   # Trettondedag jul
        e - _timedelta(days=2),   # Långfredagen
        e + _timedelta(days=1),   # Annandag påsk
        _date(year, 5, 1),   # Första maj
        e + _timedelta(days=39),  # Kristi himmelsfärdsdag
        _date(year, 6, 6),   # Sveriges nationaldag
        _date(year, 12, 24), # Julafton (bank closed)
        _date(year, 12, 25), # Juldagen
        _date(year, 12, 26), # Annandag jul
        _date(year, 12, 31), # Nyårsafton (bank closed)
    }
    # Midsommarafton — Friday between Jun 19–25
    for day in range(19, 26):
        d = _date(year, 6, day)
        if d.weekday() == 4:  # Friday
            holidays.add(d)
            break
    # Alla helgons dag — Saturday between Oct 31–Nov 6
    for day in range(31, 38):
        try:
            d = _date(year, 10 if day <= 31 else 11, day if day <= 31 else day - 31)
        except ValueError:
            d = _date(year, 11, day - 31)
        if d.weekday() == 5:  # Saturday
            holidays.add(d)
            break
    return holidays


def prev_banking_day(d: _date) -> _date:
    """
    Return the last banking day strictly before `d`.
    Skips weekends and Swedish bank holidays.
    """
    candidate = d - _timedelta(days=1)
    holidays = _swedish_bank_holidays(candidate.year)
    while candidate.weekday() >= 5 or candidate in holidays:
        candidate -= _timedelta(days=1)
        # refresh holidays if we cross a year boundary
        if candidate.year != (candidate + _timedelta(days=1)).year:
            holidays = _swedish_bank_holidays(candidate.year)
    return candidate


# ── PmtInf builder ───────────────────────────────────────────────────────────

def _add_pmt_inf(
    cstmr: ET.Element,
    invoices: list,
    pmt_inf_id: str,
    company_name: str,
    debtor_bban: str,
    debtor_bic: str,
    is_bg_pg: bool,
) -> None:
    """Append one PmtInf block. Payment date is derived per invoice from due_date."""
    n = len(invoices)
    ctrl = _fmt_amount(sum(float(i.amount_incl_vat or 0) for i in invoices))

    # Use the earliest payment date across invoices as the block-level date
    pay_dates = []
    for inv in invoices:
        due = getattr(inv, "due_date", None) or _date.today()
        pay_dates.append(prev_banking_day(due))
    block_pay_date = min(pay_dates).isoformat()

    pmt_inf = _sub(cstmr, "PmtInf")
    _sub(pmt_inf, "PmtInfId", pmt_inf_id)
    _sub(pmt_inf, "PmtMtd", "TRF")
    _sub(pmt_inf, "NbOfTxs", str(n))
    _sub(pmt_inf, "CtrlSum", ctrl)

    pmt_tp_inf = _sub(pmt_inf, "PmtTpInf")
    _sub(_sub(pmt_tp_inf, "SvcLvl"), "Cd", "NURG")
    _sub(_sub(pmt_tp_inf, "CtgyPurp"), "Cd", "SUPP")

    _sub(pmt_inf, "ReqdExctnDt", block_pay_date)

    # Debtor
    _sub(_sub(pmt_inf, "Dbtr"), "Nm", company_name)

    dbtr_acct = _sub(pmt_inf, "DbtrAcct")
    dbtr_othr = _sub(_sub(dbtr_acct, "Id"), "Othr")
    _sub(dbtr_othr, "Id", debtor_bban)
    _sub(_sub(dbtr_othr, "SchmeNm"), "Cd", "BBAN")

    _sub(_sub(_sub(pmt_inf, "DbtrAgt"), "FinInstnId"), "BIC", debtor_bic)

    # Transactions
    for idx, inv in enumerate(invoices, start=1):
        amount = float(inv.amount_incl_vat or 0)
        supplier = (inv.supplier_name or "Leverantör")[:70]
        end_to_end = f"{pmt_inf_id}-{idx:03d}"

        cdt_trf = _sub(pmt_inf, "CdtTrfTxInf")
        _sub(_sub(cdt_trf, "PmtId"), "EndToEndId", end_to_end)

        inst = _sub(_sub(cdt_trf, "Amt"), "InstdAmt", _fmt_amount(amount))
        inst.set("Ccy", "SEK")

        acct     = (inv.payment_account or "").strip()
        acct_typ = (inv.payment_account_type or "").lower()

        # Creditor agent
        if acct_typ == "bg":
            clr_sys = _sub(_sub(_sub(cdt_trf, "CdtrAgt"), "FinInstnId"), "ClrSysMmbId")
            _sub(_sub(clr_sys, "ClrSysId"), "Cd", "SESBA")
            _sub(clr_sys, "MmbId", "9900")
        elif acct_typ == "pg":
            clr_sys = _sub(_sub(_sub(cdt_trf, "CdtrAgt"), "FinInstnId"), "ClrSysMmbId")
            _sub(_sub(clr_sys, "ClrSysId"), "Cd", "SESBA")
            _sub(clr_sys, "MmbId", "9500")

        _sub(_sub(cdt_trf, "Cdtr"), "Nm", supplier)

        # Creditor account
        cdtr_acct_id = _sub(_sub(cdt_trf, "CdtrAcct"), "Id")
        if acct_typ == "bg":
            cdtr_othr = _sub(cdtr_acct_id, "Othr")
            _sub(cdtr_othr, "Id", _digits_only(acct))
            _sub(_sub(cdtr_othr, "SchmeNm"), "Prtry", "BGNR")
        elif acct_typ == "pg":
            cdtr_othr = _sub(cdtr_acct_id, "Othr")
            _sub(cdtr_othr, "Id", _digits_only(acct))
            _sub(_sub(cdtr_othr, "SchmeNm"), "Prtry", "PGNR")
        else:
            _sub(cdtr_acct_id, "IBAN", acct.upper().replace(" ", ""))

        # Remittance info — only use structured SCOR if OCR passes Luhn check
        ref = _digits_only(inv.payment_ref or "")
        if ref and _valid_ocr(ref):
            strd = _sub(_sub(cdt_trf, "RmtInf"), "Strd")
            cdtr_ref_inf = _sub(strd, "CdtrRefInf")
            _sub(_sub(_sub(cdtr_ref_inf, "Tp"), "CdOrPrtry"), "Cd", "SCOR")
            _sub(cdtr_ref_inf, "Ref", ref)
        else:
            # Fall back to unstructured: use payment_ref if present, else invoice number
            ustrd_text = (inv.payment_ref or "")[:140]
            if ustrd_text:
                _sub(_sub(cdt_trf, "RmtInf"), "Ustrd", ustrd_text)


# ── Public API ────────────────────────────────────────────────────────────────

def get_execution_date(invoices: list) -> _date:
    """
    Return the earliest ReqdExctnDt across all invoices — the same date the
    pain.001 will use for the earliest PmtInf block. Use this to store on
    PaymentFile so the Fortnox voucher gets the correct date.
    """
    dates = []
    for inv in invoices:
        due = getattr(inv, "due_date", None) or _date.today()
        dates.append(prev_banking_day(due))
    return min(dates) if dates else _date.today()


def generate_pain001(
    invoices: list,
    payment_date: _date,   # used only for message ID; actual dates derived from due_date
    config: dict,
) -> bytes:
    """
    Generate an ISO 20022 pain.001.001.03 XML payment file.

    ReqdExctnDt per PmtInf block = last banking day before the earliest due_date
    in that block. Use get_execution_date(invoices) to get the overall earliest
    date for storing on PaymentFile / Fortnox voucher.
    BG/PG and IBAN invoices are placed in separate PmtInf blocks.

    Returns UTF-8 encoded XML bytes.
    """
    company_name = config.get("COMPANY_NAME", "")
    debtor_bban  = _digits_only(config.get("COMPANY_BBAN", ""))
    if not debtor_bban:
        iban = config.get("COMPANY_IBAN", "").replace(" ", "").upper()
        debtor_bban = _digits_only(iban[4:]) if iban.startswith("SE") else ""
    debtor_bic   = config.get("COMPANY_BIC", "HANDSESS")
    org_nr       = _digits_only(config.get("COMPANY_ORG_NR", ""))

    msg_id      = f"ONEDESK-{payment_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    creation_dt = _datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    bg_pg_invs = [i for i in invoices if i.payment_account_type in ("bg", "pg")]
    iban_invs  = [i for i in invoices if i.payment_account_type == "iban"]

    n_txns   = len(bg_pg_invs) + len(iban_invs)
    ctrl_sum = _fmt_amount(
        sum(float(i.amount_incl_vat or 0) for i in bg_pg_invs + iban_invs)
    )

    root  = ET.Element(_t("Document"))
    cstmr = _sub(root, "CstmrCdtTrfInitn")

    # Group Header
    grp_hdr = _sub(cstmr, "GrpHdr")
    _sub(grp_hdr, "MsgId",   msg_id)
    _sub(grp_hdr, "CreDtTm", creation_dt)
    _sub(grp_hdr, "NbOfTxs", str(n_txns))
    _sub(grp_hdr, "CtrlSum", ctrl_sum)
    initg_pty = _sub(grp_hdr, "InitgPty")
    _sub(initg_pty, "Nm", company_name)
    if org_nr:
        _sub(_sub(_sub(_sub(initg_pty, "Id"), "OrgId"), "Othr"), "Id", org_nr)

    if bg_pg_invs:
        _add_pmt_inf(
            cstmr, bg_pg_invs,
            pmt_inf_id=f"{msg_id}-BG",
            company_name=company_name,
            debtor_bban=debtor_bban,
            debtor_bic=debtor_bic,
            is_bg_pg=True,
        )

    if iban_invs:
        _add_pmt_inf(
            cstmr, iban_invs,
            pmt_inf_id=f"{msg_id}-IBAN",
            company_name=company_name,
            debtor_bban=debtor_bban,
            debtor_bic=debtor_bic,
            is_bg_pg=False,
        )

    xml_str = ET.tostring(root, encoding="unicode", xml_declaration=False)
    decl    = '<?xml version="1.0" encoding="UTF-8"?>\n'
    return _clean_xml(decl + xml_str)
