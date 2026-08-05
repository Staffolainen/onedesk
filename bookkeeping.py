"""
Bookkeeping rules for supplier invoice vouchers (BAS chart of accounts).

The voucher rows are built here — once — so the live Fortnox client and the
dry-run preview can never drift apart. Expense vouchers still build their rows
in fortnox.py; they share the reverse-charge constants defined below.
"""

# ── VAT treatments ────────────────────────────────────────────────────────────
# Set per supplier invoice. Never keyed on supplier name.
VAT_DOMESTIC = "domestic"
VAT_REVERSE_CHARGE_DOMESTIC = "reverse_charge_domestic"
VAT_REVERSE_CHARGE_EU_SERVICES = "reverse_charge_eu_services"

VAT_TREATMENTS = (
    VAT_DOMESTIC,
    VAT_REVERSE_CHARGE_DOMESTIC,
    VAT_REVERSE_CHARGE_EU_SERVICES,
)

# The treatments where the supplier invoices without VAT and the buyer
# self-assesses it — net equals the amount actually paid.
REVERSE_CHARGE_TREATMENTS = (
    VAT_REVERSE_CHARGE_DOMESTIC,
    VAT_REVERSE_CHARGE_EU_SERVICES,
)

# ── Accounts ──────────────────────────────────────────────────────────────────
ACCOUNT_SUPPLIER_DEBT = 2440   # Leverantörsskulder
ACCOUNT_INPUT_VAT = 2641       # Debiterad ingående moms
ACCOUNT_ROUNDING = 3740        # Öresutjämning
DEFAULT_DEBIT_ACCOUNT = 6540   # fallback cost account

# Omvänd skattskyldighet, Sverige (domestic reverse charge)
REVERSE_CHARGE_VAT_RATE = 25.0
REVERSE_CHARGE_OUTPUT_ACCOUNT = 2614  # Utgående moms omvänd skattskyldighet, 25%
REVERSE_CHARGE_INPUT_ACCOUNT = 2647   # Ingående moms omvänd skattskyldighet Sverige

# Omvänd skattskyldighet, tjänster från annat EU-land (ML 5 kap. 5 §).
# The cost accounts carry the VAT report codes that fill box 20–24 of the
# VAT return — booking the cost on a plain 5xxx/6xxx account leaves box 21
# empty and Fortnox blocks the submission.
EU_SERVICES_COST_ACCOUNT_25 = 4535   # Inköp av tjänster från annat EU-land, 25% (box 21)
EU_SERVICES_COST_ACCOUNT_12 = 4536   # Inköp av tjänster från annat EU-land, 12% (box 21)
EU_SERVICES_OUTPUT_ACCOUNT_25 = 2614  # Utgående moms omvänd skattskyldighet, 25% (box 30)
EU_SERVICES_OUTPUT_ACCOUNT_12 = 2624  # Beräknad utgående moms omvänd skattskyldighet, 12% (box 31)
EU_SERVICES_INPUT_ACCOUNT = 2645      # Beräknad ingående moms på förvärv från utlandet (box 48)
EU_SERVICES_RATE_25 = 25.0
EU_SERVICES_RATE_12 = 12.0


def _f(value) -> float:
    return float(value or 0)


def _round2(value) -> float:
    return round(_f(value), 2)


def reverse_charge_vat(amount_excl) -> float:
    """Virtual VAT self-assessed on a domestic reverse-charge purchase."""
    return round(_f(amount_excl) * REVERSE_CHARGE_VAT_RATE / 100.0, 2)


def vat_treatment_of(inv) -> str:
    """VAT treatment of a supplier invoice, tolerant of rows predating the column."""
    treatment = (getattr(inv, "vat_treatment", None) or "").strip()
    if treatment in VAT_TREATMENTS:
        return treatment
    # Legacy rows carry only the boolean — those were all booked as domestic.
    if getattr(inv, "reverse_charge", False):
        return VAT_REVERSE_CHARGE_DOMESTIC
    return VAT_DOMESTIC


def is_reverse_charge(treatment) -> bool:
    return treatment in REVERSE_CHARGE_TREATMENTS


def _row(account, debit, credit, label):
    return {"account": int(account), "debit": _round2(debit),
            "credit": _round2(credit), "label": label}


def _cost_account(inv) -> int:
    if getattr(inv, "supplier_category", None) and inv.supplier_category.debit_account:
        return int(inv.supplier_category.debit_account)
    if getattr(inv, "account_code", None):
        return int(inv.account_code)
    return DEFAULT_DEBIT_ACCOUNT


def _balanced(rows):
    """Append an öresutjämning row when debits and credits do not meet."""
    diff = round(sum(r["debit"] for r in rows) - sum(r["credit"] for r in rows), 2)
    if diff:
        rows.append(_row(ACCOUNT_ROUNDING, max(-diff, 0), max(diff, 0),
                         "Öresutjämning"))
    return rows


def eu_services_split(inv):
    """(base 25%, base 12%) for a reverse-charge EU services invoice.

    The 12% part is the separately specified service/repair component of the
    net amount — bicycle service has been taxed at 12% since 2023-04-01.
    """
    net = _round2(getattr(inv, "amount_excl_vat", 0))
    base_12 = _round2(getattr(inv, "service_amount_excl_vat", 0))
    base_12 = min(max(base_12, 0.0), max(net, 0.0))
    return _round2(net - base_12), base_12


def _eu_services_rows(inv):
    """Reverse charge on services bought from another EU country.

    Both VAT sides are booked gross even though they cancel out: the output
    VAT (2614/2624) feeds box 30/31 and the input VAT (2645) box 48.
    """
    base_25, base_12 = eu_services_split(inv)
    vat_25 = round(base_25 * EU_SERVICES_RATE_25 / 100.0, 2)
    vat_12 = round(base_12 * EU_SERVICES_RATE_12 / 100.0, 2)

    rows = []
    if base_25:
        rows.append(_row(EU_SERVICES_COST_ACCOUNT_25, base_25, 0,
                         "Inköp av tjänster från annat EU-land, 25%"))
    if base_12:
        rows.append(_row(EU_SERVICES_COST_ACCOUNT_12, base_12, 0,
                         "Inköp av tjänster från annat EU-land, 12%"))
    rows.append(_row(ACCOUNT_SUPPLIER_DEBT, 0, getattr(inv, "amount_incl_vat", 0),
                     "Leverantörsskulder"))
    if vat_25:
        rows.append(_row(EU_SERVICES_OUTPUT_ACCOUNT_25, 0, vat_25,
                         "Utgående moms omvänd skattskyldighet, 25%"))
    if vat_12:
        rows.append(_row(EU_SERVICES_OUTPUT_ACCOUNT_12, 0, vat_12,
                         "Beräknad utgående moms omvänd skattskyldighet, 12%"))
    if vat_25 or vat_12:
        rows.append(_row(EU_SERVICES_INPUT_ACCOUNT, vat_25 + vat_12, 0,
                         "Beräknad ingående moms på förvärv från utlandet"))
    return rows


def _domestic_rows(inv, treatment):
    rows = [_row(_cost_account(inv), getattr(inv, "amount_excl_vat", 0), 0,
                 "Kostnadskonto / Expense account")]
    if _f(getattr(inv, "vat_amount", 0)) > 0:
        rows.append(_row(ACCOUNT_INPUT_VAT, inv.vat_amount, 0,
                         "Debiterad ingående moms"))
    if treatment == VAT_REVERSE_CHARGE_DOMESTIC:
        # The two rows cancel, so 2440 — and therefore the payment — stays at
        # the invoice sum.
        virtual_vat = reverse_charge_vat(getattr(inv, "amount_excl_vat", 0))
        rows.append(_row(REVERSE_CHARGE_INPUT_ACCOUNT, virtual_vat, 0,
                         "Ingående moms omvänd skattskyldighet"))
        rows.append(_row(REVERSE_CHARGE_OUTPUT_ACCOUNT, 0, virtual_vat,
                         "Utgående moms omvänd skattskyldighet"))
    rows.append(_row(ACCOUNT_SUPPLIER_DEBT, 0, getattr(inv, "amount_incl_vat", 0),
                     "Leverantörsskulder (betalas vid betalfil)"))
    return rows


def supplier_invoice_rows(inv):
    """Voucher rows for a supplier invoice as [{account, debit, credit, label}].

    Balanced by construction — an öresutjämning row on 3740 is appended if the
    amounts on the invoice do not quite meet.
    """
    treatment = vat_treatment_of(inv)
    if treatment == VAT_REVERSE_CHARGE_EU_SERVICES:
        return _balanced(_eu_services_rows(inv))
    return _balanced(_domestic_rows(inv, treatment))
