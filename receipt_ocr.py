"""
Receipt OCR — extracts merchant, date, amounts and VAT from receipt images.
Supports two backends (configured via .env):
  - Anthropic Claude Vision  (set ANTHROPIC_API_KEY)
  - Ollama local vision model (set OLLAMA_MODEL, e.g. llama3.2-vision)
Anthropic is tried first if both are configured.
"""
import base64
import json
import os
import re
from datetime import date


SUPPLIER_INVOICE_PROMPT = """You are analyzing a supplier invoice (PDF or image). Extract the following information and return ONLY a JSON object with no other text:

{
  "supplier_name": "company name of the sender/supplier",
  "supplier_org_nr": "Swedish org number like 556123-4567, or null",
  "invoice_date": "YYYY-MM-DD format, or null",
  "due_date": "YYYY-MM-DD payment due date/förfallodatum, or null",
  "amount_excl_vat": 1000.00,
  "vat_amount": 250.00,
  "amount_incl_vat": 1250.00,
  "currency": "SEK",
  "payment_ref": "OCR number or payment reference, digits only, or null",
  "bankgiro": "BG number like 1234-5678, or null",
  "plusgiro": "PG number, or null",
  "iban": "IBAN number if present, or null"
}

Rules:
- amount_incl_vat is the total to pay including VAT (look for 'Totalt att betala', 'Total', 'Att betala')
- payment_ref is the OCR/reference number to use when paying (often labeled 'OCR', 'Referensnummer', 'Betalningsreferens')
- bankgiro format: keep hyphens (e.g. '1234-5678')
- If a field cannot be determined, use null
- All amounts as numbers, not strings
"""

PROMPT = """You are analyzing a receipt or invoice image. Extract the following information and return ONLY a JSON object with no other text:

{
  "merchant": "store or company name",
  "date": "YYYY-MM-DD format, or null if not found",
  "amount_incl_vat": 123.45,
  "vat_rate": 25,
  "vat_amount": 24.69,
  "amount_excl_vat": 98.76,
  "currency": "SEK",
  "description": "brief description of what was purchased"
}

Rules:
- amount_incl_vat is the total amount paid including VAT (look for "Total", "Totalt", "Att betala")
- vat_rate is the VAT percentage (Swedish standard is 25%, food is 12%, books/transport is 6%)
- If you see multiple VAT rates, use the dominant one
- currency should be SEK unless clearly stated otherwise
- If a field cannot be determined, use null
- All amounts as numbers, not strings
"""


def _parse_json(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def _extract_anthropic(image_path: str, api_key: str) -> dict:
    import anthropic

    ext = image_path.rsplit(".", 1)[-1].lower()
    is_pdf = ext == "pdf"

    with open(image_path, "rb") as f:
        file_data = base64.standard_b64encode(f.read()).decode("utf-8")

    if is_pdf:
        content_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_data}}
    else:
        media_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif", "webp": "image/webp",
        }.get(ext, "image/jpeg")
        content_block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": file_data}}

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                content_block,
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    raw = message.content[0].text
    data = _parse_json(raw)
    data["raw"] = raw
    return data


def _extract_ollama(image_path: str, model: str, base_url: str) -> dict:
    """Use Ollama's OpenAI-compatible API with a local vision model."""
    from openai import OpenAI

    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    ext = image_path.rsplit(".", 1)[-1].lower()
    media_type = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "image/jpeg")

    client = OpenAI(base_url=base_url, api_key="ollama")
    response = client.chat.completions.create(
        model=model,
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_data}"}},
            ],
        }],
    )
    raw = response.choices[0].message.content
    data = _parse_json(raw)
    data["raw"] = raw
    return data


def extract_receipt_data(image_path: str, api_key: str) -> dict:
    """
    Extract structured data from a receipt image or PDF.
    For PDFs: tries regex extraction first (no API needed), then Anthropic.
    For images: tries Anthropic first, then Ollama.
    """
    is_pdf = image_path.rsplit(".", 1)[-1].lower() == "pdf"

    # PDF: try regex extraction first (born-digital PDFs, no API required)
    if is_pdf:
        result = _extract_receipt_pdf_regex(image_path)
        if not result.get("error"):
            return result
        print(f"Receipt PDF regex partial: {result.get('error')} — trying AI next")

    # Anthropic (handles both images and PDFs)
    if api_key:
        try:
            return _extract_anthropic(image_path, api_key)
        except Exception as e:
            print(f"OCR Anthropic error: {e}")

    # Ollama fallback (images only)
    ollama_model = os.getenv("OLLAMA_MODEL", "")
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    if ollama_model and not is_pdf:
        try:
            return _extract_ollama(image_path, ollama_model, ollama_url)
        except Exception as e:
            print(f"OCR Ollama error: {e}")
            return _empty_result(error=str(e))

    # For PDFs with no API: return best-effort regex result even if incomplete
    if is_pdf:
        return _extract_receipt_pdf_regex(image_path)

    return _empty_result()


def _empty_result(error=None):
    return {
        "merchant": None,
        "date": date.today().isoformat(),
        "amount_incl_vat": None,
        "vat_rate": 25,
        "vat_amount": None,
        "amount_excl_vat": None,
        "currency": "SEK",
        "description": None,
        "raw": None,
        "error": error,
    }


def _normalize_pdf_text(text: str) -> str:
    """
    Collapse whitespace that pypdf inserts within numbers and dates.
    Runs until stable. Examples:
      '20 25 -12-1 7'  → '2025-12-17'
      '50 93 -41 08'   → '5093-4108'
      '11 3 83 ,00'    → '11383,00'
    """
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r'(\d) +(\d)', r'\1\2', text)          # '20 25' → '2025'
        text = re.sub(r'(\d) +([-,\.])', r'\1\2', text)       # '83 ,00' → '83,00'
        text = re.sub(r'([-,.]) +(\d)', r'\1\2', text)        # '- 12' → '-12'
    return text


def _extract_pdf_regex(file_path: str) -> dict:
    """Extract supplier invoice fields from a PDF using text extraction + regex.
    Works on born-digital PDFs without any AI or network calls.
    """
    from pypdf import PdfReader
    try:
        reader = PdfReader(file_path, strict=False)
        text_raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        return _empty_supplier_result(error=f"PDF text extraction failed: {e}")

    if not text_raw.strip():
        return _empty_supplier_result(error="PDF contains no extractable text (scanned image?)")

    # Normalize inter-digit spaces; keep raw text for debug display
    text = _normalize_pdf_text(text_raw)

    def _find(patterns, text, group=1):
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(group).strip()
        return None

    def _parse_amount(s):
        if not s:
            return None
        # Strip all whitespace (spaces, newlines pypdf may embed in numbers)
        s = re.sub(r"\s+", "", s.strip())
        s = re.sub(r"[^\d,\.]", "", s)
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    # ── Dates ───────────────────────────────────────────────────────────────
    DATE_PAT = r"\d{4}-\d{2}-\d{2}"
    invoice_date = _find([
        r"(?:fakturadatum|invoice date)[:\s]*("+DATE_PAT+")",
        r"(?:datum)[:\s]*("+DATE_PAT+")",
    ], text)
    due_date = _find([
        r"(?:f[öo]rfallodatum|f[öo]rfall|betala senast|due date|payment due)[:\s]*("+DATE_PAT+")",
        r"(?:sista\s+betalningsdag)[:\s]*("+DATE_PAT+")",
        # 'Betalning hos oss senast' sometimes appears fully merged: 'Betalninghososssenast'
        r"betalning\s*hos\s*oss\s*senast[:\s]*("+DATE_PAT+")",
    ], text)

    # ── Invoice number ───────────────────────────────────────────────────────
    # pypdf sometimes splits numbers across lines in columnar layouts; allow
    # optional embedded whitespace then strip it from the result.
    # ── Supplier / Org.nr ────────────────────────────────────────────────────
    supplier_org_nr = _find([
        r"(?:org\.?\s*nr\.?|organisationsnummer)[:\s]*((?:\d{6}|\d{10})[-\s]?\d{0,4})",
    ], text)
    # Supplier name: first non-empty line is usually the sender
    supplier_name = None
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 3 and not re.match(r"^\d", line):
            supplier_name = line
            break

    # ── Amounts ──────────────────────────────────────────────────────────────
    # Allow \s in the amount capture so numbers split across lines by pypdf are
    # still captured; _parse_amount strips all non-numeric chars before parsing.
    AMT = r"([\d\s]+[,\.]?\d{0,2})"
    # Skip optional currency code (SEK/EUR/kr) between keyword and number
    CUR = r"(?:[A-Z]{3}|kr)?\s*"
    amount_incl_vat = _parse_amount(_find([
        # "Su m m a a tt be ta la SE K" — pypdf splits chars; match with \s* between each letter
        r"s\s*u\s*m\s*m\s*a\s+a\s*t\s*t\s+b\s*e\s*t\s*a\s*l\s*a\s+(?:[A-Z]\s*[A-Z]\s*[A-Z])?\s*([\d\s,\.]+[,\.]\d{2})",
        r"(?:summa\s+att\s+betala|totalt\s+att\s+betala|att\s+betala|to\s+pay)[:\s]*"+CUR+AMT,
        r"(?:summa\s+inkl\.?\s*moms)[:\s]*"+CUR+AMT,
        r"(?:total)[:\s]*"+CUR+AMT,
    ], text))
    vat_amount = _parse_amount(_find([
        r"(?:varav\s+moms|moms\s+\d+\s*%|mervärdesskatt|vat\s+amount)[:\s]*"+AMT,
        # "Mo ms" — pypdf splits 'Moms' with a space; anchor to line start to avoid matching "exkl. moms"
        r"(?m)^\s*m\s*o\s*m\s*s\s+([\d\s,\.]+[,\.]\d{2})",
        r"(?:moms)[:\s]*([\d\s]+[,\.]\d{2})\s*(?:kr|sek)?",
    ], text))
    amount_excl_vat = _parse_amount(_find([
        r"(?:summa\s+exkl\.?\s*moms|netto|exkl\.?\s*moms|excl\.?\s*vat)[:\s]*"+AMT,
    ], text))
    if amount_incl_vat and vat_amount and not amount_excl_vat:
        amount_excl_vat = round(amount_incl_vat - vat_amount, 2)

    # ── Payment info — BG takes priority, then PG, IBAN only as last resort ──
    bankgiro = _find([
        # "Bankgiro" or "Bankgironr" or "Till bankgiro" followed by number
        r"(?:(?:till\s+)?bankgiro(?:\s*nr)?)[:\s]*((?:\d[\d\s]{1,5}-\d{4}|\d{7,8}))",
        r"\bbg[:\s]*((?:\d{3,4}-\d{4}|\d{7,8}))",
    ], text)
    if bankgiro:
        # Normalise: strip embedded spaces from "5 093-4108" → "5093-4108"
        bankgiro = re.sub(r"\s+", "", bankgiro)
    if bankgiro:
        plusgiro = None
        iban = None
    else:
        plusgiro = _find([
            r"(?:plusgiro|pg)[:\s]*((?:\d{1,7}-\d|\d{2,8}))",
        ], text)
        iban = None if plusgiro else _find([
            r"\b(SE\d{2}[\d\s]{20,30})\b",
        ], text)
    payment_ref = _find([
        # "OCR-nummer", "OCR nummer", "OCR" — handle hyphen variant
        r"(?:ocr(?:-?nummer)?|referens(?:nummer)?|betalningsreferens|payment\s*ref)[:\s]*([\d]{4,30})",
        # "Fakturanummer" is often identical to the OCR/payment ref
        r"fakturanummer\s*[:\s]*([\d]{4,20})",
    ], text)
    if payment_ref:
        payment_ref = re.sub(r"\s+", "", payment_ref)

    result = {
        "supplier_name": supplier_name,
        "supplier_org_nr": supplier_org_nr,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "amount_excl_vat": amount_excl_vat,
        "vat_amount": vat_amount,
        "amount_incl_vat": amount_incl_vat,
        "currency": "SEK",
        "payment_ref": payment_ref,
        "bankgiro": bankgiro,
        "plusgiro": plusgiro,
        "iban": iban,
        "raw": text_raw[:2000],
        "error": None,
    }
    # Consider it a success if we got at least an amount or a BG/PG number
    if not any([amount_incl_vat, bankgiro, plusgiro, payment_ref]):
        result["error"] = "PDF parsed but key fields not found — please fill in manually"
    return result


def _extract_receipt_pdf_regex(file_path: str) -> dict:
    """Extract expense receipt fields from a PDF using text extraction + regex.
    Works on born-digital PDFs without any AI or network calls.
    Maps to the expense schema: merchant, date, amounts, vat_rate, currency.
    """
    from pypdf import PdfReader
    try:
        reader = PdfReader(file_path, strict=False)
        text_raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        return _empty_result(error=f"PDF text extraction failed: {e}")

    if not text_raw.strip():
        return _empty_result(error="PDF contains no extractable text (scanned image?)")

    text = _normalize_pdf_text(text_raw)

    def _find(patterns, src, group=1):
        for pat in patterns:
            m = re.search(pat, src, re.IGNORECASE)
            if m:
                return m.group(group).strip()
        return None

    def _parse_amount(s):
        if not s:
            return None
        s = re.sub(r"\s+", "", s.strip())
        s = re.sub(r"[^\d,\.]", "", s)
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    AMT = r"([\d\s]+[,\.]?\d{0,2})"
    CUR = r"(?:[A-Z]{3}|kr)?\s*"
    DATE_ISO = r"\d{4}-\d{2}-\d{2}"
    DATE_DMY = r"(\d{2})/(\d{2})/(\d{4})"   # DD/MM/YYYY (e.g. Microsoft invoices)

    def _parse_date(text):
        """Return first date found as YYYY-MM-DD. Tries labeled patterns first, then bare dates."""
        # Labeled ISO: "datum 2025-05-01"
        for pat in [
            r"(?:kvittodatum|inköpsdatum|köpdatum|receipt date|purchase date)[:\s]*("+DATE_ISO+")",
            r"(?:datum)[:\s]*("+DATE_ISO+")",
            r"(?:fakturadatum|invoice date)[:\s]*("+DATE_ISO+")",
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1)
        # Labeled DD/MM/YYYY: "Förfaller 09/06/2025"
        for pat in [
            r"(?:f[öo]rfaller|due|betala\s+senast|f[öo]rfallodatum)[:\s]*"+DATE_DMY,
            r"(?:datum|date)[:\s]*"+DATE_DMY,
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        # Bare ISO date
        m = re.search(r"\b("+DATE_ISO+r")\b", text)
        if m:
            return m.group(1)
        # Bare DD/MM/YYYY
        m = re.search(DATE_DMY, text)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        return None

    # Currency: detect from explicit label, default SEK
    cur_match = re.search(r"\b(SEK|EUR|USD|GBP|NOK|DKK)\b", text)
    currency = cur_match.group(1) if cur_match else "SEK"

    # Merchant/supplier name
    merchant = None
    # Try labeled sections first: name appears on the line after the header
    for header_pat in [
        r"sammanfattning\s+av\s+fakturering",
        r"faktureras\s+till",
        r"bill\s+to",
        r"sold\s+to",
    ]:
        m = re.search(header_pat, text, re.IGNORECASE)
        if m:
            rest = text[m.end():]
            for line in rest.splitlines():
                line = line.strip()
                if len(line) > 3 and not re.match(r"^\d", line):
                    merchant = line
                    break
            if merchant:
                break
    # Fallback: first non-empty, non-numeric line in the whole document
    if not merchant:
        for line in text.splitlines():
            line = line.strip()
            if len(line) > 3 and not re.match(r"^\d", line):
                merchant = line
                break

    receipt_date = _parse_date(text)

    amount_incl_vat = _parse_amount(_find([
        # Microsoft: "Totalsumma (inklusive moms) SEK 3,48"
        r"totalsumma\s*\([^)]*moms[^)]*\)\s*(?:[A-Z]{3})?\s*([\d\s,\.]+[,\.]\d{2})",
        r"s\s*u\s*m\s*m\s*a\s+a\s*t\s*t\s+b\s*e\s*t\s*a\s*l\s*a\s+(?:[A-Z]\s*[A-Z]\s*[A-Z])?\s*([\d\s,\.]+[,\.]\d{2})",
        r"(?:summa\s+att\s+betala|totalt\s+att\s+betala|att\s+betala|to\s+pay)[:\s]*"+CUR+AMT,
        r"(?:summa\s+inkl\.?\s*moms)[:\s]*"+CUR+AMT,
        r"(?:total\s+inkl|total)[:\s]*"+CUR+AMT,
        r"(?:summa|totalt)[:\s]*"+CUR+AMT,
    ], text))

    vat_amount = _parse_amount(_find([
        # Microsoft: "Skatt 0,70"
        r"(?m)^skatt\s+([\d\s,\.]+[,\.]\d{2})",
        r"(?:varav\s+moms|moms\s+\d+\s*%|mervärdesskatt|vat\s+amount)[:\s]*"+AMT,
        r"(?m)^\s*m\s*o\s*m\s*s\s+([\d\s,\.]+[,\.]\d{2})",
        r"(?:moms)[:\s]*([\d\s]+[,\.]\d{2})\s*(?:kr|sek)?",
    ], text))

    amount_excl_vat = _parse_amount(_find([
        # Microsoft: "Debitering 2,78"
        r"(?m)^debitering\s+([\d\s,\.]+[,\.]\d{2})",
        r"(?:summa\s+exkl\.?\s*moms|netto|exkl\.?\s*moms|excl\.?\s*vat)[:\s]*"+AMT,
    ], text))
    if amount_incl_vat and vat_amount and not amount_excl_vat:
        amount_excl_vat = round(amount_incl_vat - vat_amount, 2)

    vat_rate = None
    if amount_excl_vat and vat_amount and amount_excl_vat > 0:
        vat_rate = round(vat_amount / amount_excl_vat * 100, 1)
    if not vat_rate:
        vat_rate = _find([r"moms\s+(\d+)\s*%", r"vat\s+(\d+)\s*%"], text)
        vat_rate = float(vat_rate) if vat_rate else 25

    result = {
        "merchant": merchant,
        "date": receipt_date or date.today().isoformat(),
        "amount_incl_vat": amount_incl_vat,
        "vat_amount": vat_amount,
        "amount_excl_vat": amount_excl_vat,
        "vat_rate": vat_rate,
        "currency": currency,
        "description": None,
        "raw": text_raw[:3000],
        "error": None,
    }
    if not amount_incl_vat:
        result["error"] = "PDF parsed but no total amount found — please fill in manually"
    return result


def _extract_supplier_invoice_ollama(file_path: str, model: str, base_url: str) -> dict:
    """Extract supplier invoice data using a local Ollama vision model."""
    from openai import OpenAI

    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return _empty_supplier_result(error="Ollama does not support PDF — convert to image first")

    with open(file_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    media_type = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "image/jpeg")

    client = OpenAI(base_url=base_url, api_key="ollama")
    response = client.chat.completions.create(
        model=model,
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": SUPPLIER_INVOICE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_data}"}},
            ],
        }],
    )
    raw = response.choices[0].message.content
    data = _parse_json(raw)
    data["raw"] = raw
    return data


def _extract_supplier_invoice_anthropic(file_path: str, api_key: str) -> dict:
    """Extract supplier invoice data using Claude Vision. Handles both images and PDFs."""
    import anthropic

    ext = file_path.rsplit(".", 1)[-1].lower()
    is_pdf = ext == "pdf"

    with open(file_path, "rb") as f:
        file_data = base64.standard_b64encode(f.read()).decode("utf-8")

    if is_pdf:
        source = {
            "type": "base64",
            "media_type": "application/pdf",
            "data": file_data,
        }
        content_block = {"type": "document", "source": source}
    else:
        media_type = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif", "webp": "image/webp",
        }.get(ext, "image/jpeg")
        source = {"type": "base64", "media_type": media_type, "data": file_data}
        content_block = {"type": "image", "source": source}

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                content_block,
                {"type": "text", "text": SUPPLIER_INVOICE_PROMPT},
            ],
        }],
    )
    raw = message.content[0].text
    data = _parse_json(raw)
    data["raw"] = raw
    return data


def _empty_supplier_result(error=None):
    return {
        "supplier_name": None, "supplier_org_nr": None,
        "invoice_date": None, "due_date": None,
        "amount_excl_vat": None, "vat_amount": None, "amount_incl_vat": None,
        "currency": "SEK", "payment_ref": None,
        "bankgiro": None, "plusgiro": None, "iban": None,  # kept for OCR parsing; mapped to payment_account on save
        "raw": None, "error": error or "OCR not available",
    }


def extract_supplier_invoice_data(file_path: str, api_key: str) -> dict:
    """Extract structured data from a supplier invoice PDF or image.

    Priority:
    1. PDF regex  — instant, no AI, works for born-digital PDFs
    2. Anthropic  — best quality, handles scanned PDFs and images (if api_key set)
    3. Ollama     — local fallback for images (if OLLAMA_MODEL set)
    """
    is_pdf = file_path.rsplit(".", 1)[-1].lower() == "pdf"

    # 1. PDF text extraction + regex (PDFs only)
    if is_pdf:
        result = _extract_pdf_regex(file_path)
        # Use regex result if it found useful data; fall through to AI otherwise
        if not result.get("error"):
            return result
        print(f"PDF regex partial: {result.get('error')} — trying AI next")

    # 2. Anthropic
    if api_key:
        try:
            return _extract_supplier_invoice_anthropic(file_path, api_key)
        except Exception as e:
            print(f"Supplier invoice OCR Anthropic error: {e}")

    # 3. Ollama (images only)
    ollama_model = os.getenv("OLLAMA_MODEL", "")
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    if ollama_model and not is_pdf:
        try:
            return _extract_supplier_invoice_ollama(file_path, ollama_model, ollama_url)
        except Exception as e:
            print(f"Supplier invoice OCR Ollama error: {e}")
            return _empty_supplier_result(error=str(e))

    # Return best-effort regex result even if incomplete, rather than nothing
    if is_pdf:
        return _extract_pdf_regex(file_path)

    return _empty_supplier_result()
