import json
import re
import requests
from config import Config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
GEMINI_TIMEOUT = 15

INVOICE_PROMPT = """You are an expert at extracting structured data from Malaysian SME business documents.
Below is OCR-extracted text from an INVOICE document. Extract the fields listed.

IMPORTANT RULES:
- The VENDOR is the entity ISSUING the invoice (the seller), typically shown as the company name in the document header at the top
- The BUYER is who the invoice is billed TO ("Bill To", "Invoice To"), which is NOT the vendor
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD
- Return ONLY valid JSON, no markdown, no explanation, no code fences

OCR TEXT:
---
{ocr_text}
---

Return this exact JSON structure:
{{
  "invoice_number": "string or null",
  "vendor_name": "string or null (the SELLER shown in the header)",
  "invoice_date": "YYYY-MM-DD or null",
  "total_amount": number or null,
  "tax_amount": number or null
}}"""

PO_PROMPT = """You are an expert at extracting structured data from Malaysian SME business documents.
Below is OCR-extracted text from a PURCHASE ORDER (PO) document. Extract the fields listed.

IMPORTANT RULES:
- The VENDOR is the SUPPLIER the PO is issued TO (look for labels like "Bill To Vendor", "Vendor:", "Supplier:", "To:")
- The company shown in the header of a PO is usually the BUYER issuing the order, NOT the vendor
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD
- Return ONLY valid JSON, no markdown, no explanation, no code fences

OCR TEXT:
---
{ocr_text}
---

Return this exact JSON structure:
{{
  "po_number": "string or null",
  "vendor_name": "string or null (the SUPPLIER the PO is issued to)",
  "po_date": "YYYY-MM-DD or null",
  "total_amount": number or null
}}"""

GR_PROMPT = """You are an expert at extracting structured data from Malaysian SME business documents.
Below is OCR-extracted text from a GOODS RECEIPT (GR) document. Extract the fields listed.

IMPORTANT RULES:
- The VENDOR is who DELIVERED the goods (look for "Received From", "Delivered by", "Supplier")
- The company shown in the header is usually the RECEIVING company (the buyer's warehouse), NOT the vendor
- Return null for any field you cannot confidently extract
- Amounts must be numbers only (no currency symbols, no commas, no "RM")
- Dates in ISO format: YYYY-MM-DD
- Return ONLY valid JSON, no markdown, no explanation, no code fences

OCR TEXT:
---
{ocr_text}
---

Return this exact JSON structure:
{{
  "gr_number": "string or null",
  "vendor_name": "string or null (who DELIVERED the goods)",
  "receipt_date": "YYYY-MM-DD or null"
}}"""


def _strip_markdown_fences(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _call_gemini(template, ocr_text):
    if not Config.GEMINI_API_KEY:
        print("DEBUG Gemini: GEMINI_API_KEY not set, skipping")
        return {}

    try:
        prompt = template.format(ocr_text=ocr_text)
        # Key goes in a header, never the URL, so it can't leak into
        # exception messages, proxy logs, or redirect chains.
        headers = {
            'Content-Type': 'application/json',
            'x-goog-api-key': Config.GEMINI_API_KEY
        }
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json"
            }
        }
        response = requests.post(GEMINI_URL, json=payload, headers=headers, timeout=GEMINI_TIMEOUT)
        response.raise_for_status()
        result = response.json()

        text = result['candidates'][0]['content']['parts'][0]['text']
        text = _strip_markdown_fences(text)
        return json.loads(text)

    except Exception as e:
        print(f"DEBUG Gemini call error: {type(e).__name__}: {e}")
        return {}


def gemini_extract_invoice(ocr_text):
    result = _call_gemini(INVOICE_PROMPT, ocr_text)
    print(f"DEBUG Gemini extracted invoice: {result}")
    return result


def gemini_extract_po(ocr_text):
    result = _call_gemini(PO_PROMPT, ocr_text)
    print(f"DEBUG Gemini extracted po: {result}")
    return result


def gemini_extract_gr(ocr_text):
    result = _call_gemini(GR_PROMPT, ocr_text)
    print(f"DEBUG Gemini extracted gr: {result}")
    return result
