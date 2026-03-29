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
from datetime import date


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
