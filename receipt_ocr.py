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
  "invoice_number": "invoice number/fakturanummer, or null",
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

    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    ext = image_path.rsplit(".", 1)[-1].lower()
    media_type = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "image/jpeg")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
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
    Extract structured data from a receipt image.
    Tries Anthropic first (if api_key set), then Ollama (if OLLAMA_MODEL set).
    """
    # Anthropic
    if api_key:
        try:
            return _extract_anthropic(image_path, api_key)
        except Exception as e:
            print(f"OCR Anthropic error: {e}")

    # Ollama fallback
    ollama_model = os.getenv("OLLAMA_MODEL", "")
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    if ollama_model:
        try:
            return _extract_ollama(image_path, ollama_model, ollama_url)
        except Exception as e:
            print(f"OCR Ollama error: {e}")
            return _empty_result(error=str(e))

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


def _extract_pdf_regex(file_path: str) -> dict:
    """Extract supplier invoice fields from a PDF using text extraction + regex.
    Works on born-digital PDFs without any AI or network calls.
    """
    from pypdf import PdfReader
    try:
        reader = PdfReader(file_path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        return _empty_supplier_result(error=f"PDF text extraction failed: {e}")

    if not text.strip():
        return _empty_supplier_result(error="PDF contains no extractable text (scanned image?)")

    def _find(patterns, text, group=1):
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(group).strip()
        return None

    def _parse_amount(s):
        if not s:
            return None
        # Handle Swedish decimal comma and thousand separators
        s = s.strip().replace(" ", "").replace("\xa0", "")
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
    ], text)

    # ── Invoice number ───────────────────────────────────────────────────────
    invoice_number = _find([
        r"(?:fakturanummer|faktura\s*nr\.?|invoice\s*no\.?|invoice\s*number)[:\s#]*([A-Z0-9\-\/]+)",
    ], text)

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
    AMT = r"([\d\s]+[,\.]?\d{0,2})"
    amount_incl_vat = _parse_amount(_find([
        r"(?:att betala|totalt att betala|total att betala|to pay|total)[:\s]*"+AMT,
        r"(?:summa inkl\.?\s*moms)[:\s]*"+AMT,
    ], text))
    vat_amount = _parse_amount(_find([
        r"(?:varav\s+moms|moms\s+\d+\s*%|mervärdesskatt|vat\s+amount)[:\s]*"+AMT,
        r"(?:moms)[:\s]*([\d\s]+[,\.]?\d{0,2})\s*(?:kr|sek)?",
    ], text))
    amount_excl_vat = _parse_amount(_find([
        r"(?:netto|exkl\.?\s*moms|excl\.?\s*vat)[:\s]*"+AMT,
    ], text))
    if amount_incl_vat and vat_amount and not amount_excl_vat:
        amount_excl_vat = round(amount_incl_vat - vat_amount, 2)

    # ── Payment info — BG takes priority, then PG, IBAN only as last resort ──
    bankgiro = _find([
        r"(?:bankgiro|bg)[:\s]*((?:\d{3,4}-\d{4}|\d{7,8}))",
    ], text)
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
        r"(?:ocr|referens(?:nummer)?|betalningsreferens|payment\s*ref)[:\s]*([\d\s]{4,30})",
    ], text)
    if payment_ref:
        payment_ref = re.sub(r"\s+", "", payment_ref)

    result = {
        "supplier_name": supplier_name,
        "supplier_org_nr": supplier_org_nr,
        "invoice_number": invoice_number,
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
        "raw": text[:2000],
        "error": None,
    }
    # Consider it a success if we got at least an amount or a BG/PG number
    if not any([amount_incl_vat, bankgiro, plusgiro, payment_ref]):
        result["error"] = "PDF parsed but key fields not found — please fill in manually"
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
        "invoice_number": None, "invoice_date": None, "due_date": None,
        "amount_excl_vat": None, "vat_amount": None, "amount_incl_vat": None,
        "currency": "SEK", "payment_ref": None,
        "bankgiro": None, "plusgiro": None, "iban": None,
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
