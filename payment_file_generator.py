"""
Bankgirot LB (Leverantörsbetalningar) payment file generator for Handelsbanken.

Format: fixed-width text, ISO-8859-1 encoded (Swedish banking standard).
Each record is 80 characters wide.

Record types:
  TK01 – Opening record (one per file)
  TK14 – Payment transaction (one per invoice)
  TK65 – Closing record (one per file)
"""
from datetime import date as _date


def _pad(value: str, width: int, align: str = 'left', fill: str = ' ') -> str:
    """Pad or truncate a string to exactly `width` characters."""
    value = str(value or '')
    if align == 'right':
        return value[-width:].rjust(width, fill)
    return value[:width].ljust(width, fill)


def _digits_only(value: str) -> str:
    """Remove all non-digit characters (spaces, hyphens, dots)."""
    return ''.join(c for c in str(value or '') if c.isdigit())


def _amount_ore(amount_sek: float) -> str:
    """Convert SEK float to öre integer string (e.g. 1234.50 → '123450')."""
    return str(round(amount_sek * 100))


def generate_lb_file(
    sender_bankgiro: str,
    invoices: list,          # list of SupplierInvoice objects
    payment_date: _date,
) -> bytes:
    """
    Generate a Bankgirot LB file.

    invoices: list of SupplierInvoice ORM objects. Each must have:
        bankgiro or plusgiro, payment_ref, amount_incl_vat.

    Returns the file content as bytes (ISO-8859-1 encoded).
    """
    sender_bg = _digits_only(sender_bankgiro)
    date_str = payment_date.strftime('%Y%m%d')
    records = []

    # TK01 – Opening record
    # Pos 1-2:   Record type "01"
    # Pos 3-12:  Sender bankgiro (10 digits, zero-padded, right-aligned)
    # Pos 13-20: Payment date YYYYMMDD
    # Pos 21-30: Product code "LEVERBET" + spaces
    # Pos 31-80: Filler
    tk01 = (
        '01'
        + _pad(sender_bg, 10, 'right', '0')
        + date_str
        + _pad('LEVERBET', 10)
        + _pad('', 50)
    )
    records.append(tk01)

    total_ore = 0
    for inv in invoices:
        # Determine recipient account
        if inv.bankgiro:
            recipient = _digits_only(inv.bankgiro)
            acc_type = 'BG'
        elif inv.plusgiro:
            recipient = _digits_only(inv.plusgiro)
            acc_type = 'PG'
        else:
            # No bankgiro/plusgiro — skip this invoice
            continue

        ore = round(inv.amount_incl_vat * 100)
        total_ore += ore
        ocr = _digits_only(inv.payment_ref or '')

        # TK14 – Payment transaction
        # Pos 1-2:   Record type "14"
        # Pos 3-14:  Recipient BG/PG number (12 digits, zero-padded, right-aligned)
        # Pos 15-39: OCR/reference (25 chars, right-aligned, zero-padded for OCR)
        # Pos 40-51: Amount in öre (12 digits, zero-padded, right-aligned)
        # Pos 52-59: Payment date YYYYMMDD
        # Pos 60-61: Currency code space "  " (SEK implied)
        # Pos 62-80: Filler
        tk14 = (
            '14'
            + _pad(recipient, 12, 'right', '0')
            + _pad(ocr, 25, 'right', '0')
            + _pad(str(ore), 12, 'right', '0')
            + date_str
            + '  '
            + _pad('', 19)
        )
        records.append(tk14)

    num_payments = len(records) - 1  # exclude TK01

    # TK65 – Closing record
    # Pos 1-2:   Record type "65"
    # Pos 3-10:  Number of TK14 records (8 digits, zero-padded)
    # Pos 11-22: Total amount in öre (12 digits, zero-padded)
    # Pos 23-80: Filler
    tk65 = (
        '65'
        + _pad(str(num_payments), 8, 'right', '0')
        + _pad(str(total_ore), 12, 'right', '0')
        + _pad('', 58)
    )
    records.append(tk65)

    # Each record must be exactly 80 chars
    for i, r in enumerate(records):
        assert len(r) == 80, f"Record {i} is {len(r)} chars, expected 80: {r!r}"

    content = '\r\n'.join(records) + '\r\n'
    return content.encode('iso-8859-1')
