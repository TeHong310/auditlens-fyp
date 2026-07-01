import re
import os
import base64
import tempfile
import requests
from datetime import datetime
from pdf2image import convert_from_path
from config import Config

INVOICE_BLACKLIST = ['account no', 'account number', 'deposit', 'page', 'sub total', 'total']

# Companies that are typically BUYERS (to exclude from vendor detection)
BUYER_KEYWORDS = [
    'northern point', 'invoice to', 'bill to', 'sold to', 'deliver to',
    'ship to', 'attn', 'attention'
]
def clean_vendor_name(vendor):
    if not vendor:
        return vendor
    # Remove registration numbers like 242481-M, (605195-A)
    vendor = re.sub(r'\s*[\-]?\s*\d{6,}[\-\w]*', '', vendor)
    vendor = re.sub(r'\s*\(\d{6,}[\-\w]*\)', '', vendor)
    vendor = re.sub(r'\s+', ' ', vendor).strip()
    vendor = vendor.rstrip('.-,')
    return vendor.strip()

def normalize_date_string(date_str):
    if not date_str:
        return date_str
    normalized = date_str.strip()
    month_fixes = {
        r'\b0ct\b': 'Oct', r'\b0an\b': 'Jan', r'\bFe6\b': 'Feb',
        r'\bJan\b': 'Jan', r'\bFeb\b': 'Feb', r'\bMar\b': 'Mar',
        r'\bApr\b': 'Apr', r'\bApri1\b': 'April', r'\bMay\b': 'May',
        r'\bJun\b': 'Jun', r'\bJu1\b': 'Jul', r'\bJul\b': 'Jul',
        r'\bAu9\b': 'Aug', r'\bAug\b': 'Aug', r'\b5ep\b': 'Sep',
        r'\bSep\b': 'Sep', r'\b0ec\b': 'Dec', r'\bDec\b': 'Dec',
        r'\bN0v\b': 'Nov', r'\bNov\b': 'Nov', r'\bOct\b': 'Oct',
    }
    for pattern, replacement in month_fixes.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized

def parse_date(date_str):
    if not date_str:
        return None
    normalized = normalize_date_string(date_str)
    formats = [
        '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y-%m-%d',
        '%d/%b/%Y', '%d %b %Y', '%d %B %Y', '%B %d, %Y',
        '%B %d %Y', '%b %d, %Y', '%b %d %Y', '%d-%b-%Y',
        '%d/%m/%y', '%d-%m-%y', '%d-%b-%y',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt).date()
        except:
            continue
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except:
            continue
    return None

def is_valid_invoice_number(val):
    if not val:
        return False
    val_lower = val.lower()
    invalid_words = ['date', 'invoice', 'receipt', 'total', 'amount', 'page',
                     'account', 'deposit', 'sub', 'no', 'to', 'from', 'by']
    if val_lower in invalid_words:
        return False
    for b in INVOICE_BLACKLIST:
        if b in val_lower:
            return False
    if not re.search(r'[A-Za-z0-9]', val):
        return False
    if len(val) < 3:
        return False
    return True

def extract_amount(text):
    match = re.search(r'([\d,]+\.?\d*)', str(text))
    if match:
        try:
            val = float(match.group(1).replace(',', ''))
            if val > 0 and val < 10000000:
                return val
        except:
            pass
    return None

def is_buyer_line(line):
    line_lower = line.lower()
    for keyword in BUYER_KEYWORDS:
        if keyword in line_lower:
            return True
    return False

def run_google_vision_ocr(image_path):
    with open(image_path, 'rb') as f:
        image_data = base64.b64encode(f.read()).decode('utf-8')

    url = f"https://vision.googleapis.com/v1/images:annotate?key={Config.GOOGLE_VISION_API_KEY}"
    payload = {
        "requests": [{
            "image": {"content": image_data},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}]
        }]
    }

    response = requests.post(url, json=payload)
    result   = response.json()

    if 'responses' not in result or not result['responses']:
        return '', 0.0

    response_data = result['responses'][0]

    if 'fullTextAnnotation' not in response_data:
        return '', 0.0

    full_text  = response_data['fullTextAnnotation'].get('text', '')
    confidence = 0.0

    try:
        pages = response_data['fullTextAnnotation'].get('pages', [])
        if pages:
            confidences = []
            for page in pages:
                for block in page.get('blocks', []):
                    conf = block.get('confidence', 0)
                    if conf > 0:
                        confidences.append(conf)
            if confidences:
                confidence = round(sum(confidences) / len(confidences) * 100, 2)
    except:
        confidence = 85.0

    print(f"DEBUG OCR TEXT: {full_text[:500]}")
    return full_text, confidence


def run_ocr(file_path, file_ext):
    all_results = []
    total_text  = ''
    confidence  = 0.0

    try:
        if file_ext == 'pdf':
            images = convert_from_path(file_path, poppler_path=Config.POPPLER_PATH, dpi=300)
            all_confidences = []

            for img in images:
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    tmp_path = tmp.name
                img.save(tmp_path, 'PNG')

                page_text, page_conf = run_google_vision_ocr(tmp_path)
                total_text += page_text + '\n'
                if page_conf > 0:
                    all_confidences.append(page_conf)

                try:
                    os.unlink(tmp_path)
                except:
                    pass

            if all_confidences:
                confidence = round(sum(all_confidences) / len(all_confidences), 2)

        else:
            total_text, confidence = run_google_vision_ocr(file_path)

        if total_text.strip():
            all_results = [([[0,0],[1,0],[1,1],[0,1]], total_text, confidence/100)]

    except Exception as e:
        print(f"Google Vision OCR error: {e}")

    return all_results, total_text, confidence


def calculate_confidence(ocr_results):
    if not ocr_results:
        return 0.0
    confidences = [r[2] for r in ocr_results if len(r) >= 3 and r[2] > 0]
    if confidences:
        return round(sum(confidences) / len(confidences) * 100, 2)
    return 0.0


def extract_fields(ocr_text):
    fields = {
        'invoice_number': None,
        'vendor_name':    None,
        'invoice_date':   None,
        'total_amount':   None,
        'tax_amount':     None,
    }

    lines = ocr_text.split('\n')
    full_text_lower = ocr_text.lower()

    # ── Track if we passed "Invoice To" section ──────────
    passed_invoice_to = False
    invoice_to_index  = -1
    for i, line in enumerate(lines):
        if re.search(r'invoice\s*to|bill\s*to|sold\s*to', line, re.IGNORECASE):
            invoice_to_index = i
            break

    for i, line in enumerate(lines):
        line_clean = line.strip()
        next_line  = lines[i + 1].strip() if i + 1 < len(lines) else ''

        # ── VENDOR NAME ───────────────────────────────────
        # Strategy: Find company name that is NOT the buyer
        if fields['vendor_name'] is None:
            # Look for company names (Sdn Bhd, Ltd, etc.)
            company_match = re.search(
                r'^([\w\s&\.\-\(\)\/]+(?:sdn\.?\s*bhd\.?|berhad|corporation|corp|ltd\.?|enterprise|trading|network|logistics|broadband|applications)[\w\s&\.\-\(\)\/\-]*)',
                line_clean, re.IGNORECASE
            )
            if company_match:
                vendor = company_match.group(1).strip()
                # Skip if this line is part of buyer section
                if (len(vendor) > 5 and
                    not is_buyer_line(line_clean) and
                    not is_buyer_line(lines[i-1] if i > 0 else '') and
                    not re.search(r'\b(customer|invoice\s*to|bill\s*to|deliver\s*to|ship\s*to)\b',
                                  ' '.join(lines[max(0,i-2):i]), re.IGNORECASE)):
                    fields['vendor_name'] = clean_vendor_name(vendor)

        # ── INVOICE NUMBER ────────────────────────────────
        if fields['invoice_number'] is None:
            # Skip lines that are clearly not invoice number labels
            if re.search(r'\b(?:po|p\.o|gr|goods\s*receipt|reg\.?|account|tel|fax|bank|acct)\s*(?:no\.?|number)\s*[:\-]', line_clean, re.IGNORECASE):
                pass  # skip invoice number extraction on this line
            else:
                inv_patterns = [
                    # E-invoice
                    r'e-invoice\s*no\.?\s*[:\-]?\s*(SIN\d+)',
                    r'\b(SIN\d{6,})\b',
                    # Standard No: XXXXX pattern (generic - works for any invoice)
                    r'^No\s*[:\-]\s*([A-Z]{2,}[A-Z0-9\-\/]+)',
                    r'No\s*:\s*([A-Z]{2,}[A-Z0-9\-\/]+)',
                    # Invoice No patterns
                    r'invoice\s*no\.?\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    r'invoice\s*number\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    r'receipt\s*(?:no\.?|number)\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                    r'booking\s*id\s*[:\-]?\s*([A-Z0-9\-]+)',
                    r'bill\s*(?:no\.?|number)\s*[:\-]?\s*(\S+)',
                    r'invoice\s*(?:no\.?|#)\s*[:\-]?\s*(\S+)',
                ]
                for p in inv_patterns:
                    match = re.search(p, line_clean, re.IGNORECASE)
                    if match:
                        val = match.group(1).strip().rstrip('-').strip()
                        val = re.sub(r'\s*-\s*\d+$', '', val).strip()
                        if is_valid_invoice_number(val):
                            fields['invoice_number'] = val
                            break

                # Label only, value on next line
                if fields['invoice_number'] is None:
                    if re.search(r'^(?:invoice\s*(?:no\.?|number|#)|inv\s*(?:no\.?|#)|receipt\s*(?:no\.?|number))\s*[:\-]\s*$',
                                 line_clean, re.IGNORECASE):
                        if next_line and is_valid_invoice_number(next_line.strip()):
                            fields['invoice_number'] = next_line.strip()

        # ── INVOICE DATE ──────────────────────────────────
        if fields['invoice_date'] is None:
            date_patterns = [
                # E-invoice
                r'e-invoice\s*date.*?[:\-]\s*(\d{2}-\d{2}-\d{4})',
                # Standard date patterns
                r'(?:invoice|receipt|bill)\s*date\s*[:\-]?\s*(\d{1,2}[-\/]\w+[-\/]\d{2,4})',
                r'(?:invoice|receipt|bill)\s*date\s*[:\-]?\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})',
                r'(?:invoice|receipt|bill)\s*date\s*[:\-]?\s*(\d{1,2}\s+\w+\s+\d{4})',
                r'invoice\s*due\s*date\s*[:\-]?\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})',
                # Generic date label
                r'^date\s*[:\-]\s*(\d{1,2}[-\/]\w+[-\/]\d{2,4})',
                r'^date\s*[:\-]\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})',
                r'^date\s*[:\-]\s*(\d{1,2}\s+\w+\s+\d{4})',
                r'^date\s*[:\-]\s*(\w+\s+\d{1,2},?\s+\d{4})',
                # Date/Time format
                r'date\/time\s*[:\-]?\s*(\d{1,2}[-\/]\w+[-\/]\d{4})',
                r'date\/time\s*[:\-]?\s*(\d{1,2}[-\/]\d{1,2}[-\/]\d{4})',
            ]
            for p in date_patterns:
                match = re.search(p, line_clean, re.IGNORECASE)
                if match:
                    raw_date = match.group(1).strip()
                    if len(raw_date) > 4:
                        fields['invoice_date'] = normalize_date_string(raw_date)
                        break

        # ── TOTAL AMOUNT ──────────────────────────────────
        if fields['total_amount'] is None:
            amount_patterns = [
                r'\btotal\s*\(\s*(?:rm|myr)\s*\)\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*net\s*amount\s*(?:\(rm\))?\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*payable\s*amount\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*including\s*(?:tax|sst|gst)\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'total\s*sales\s*\(inclusive\s*of\s*sst\)\s*([\d,]+\.?\d*)',
                r'total\s*paid\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'total\s*amount\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'total\s*current\s*charges\s*(?:myr|rm)?\s*([\d,]+\.?\d*)',
                r'grand\s*total\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'^total\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)$',
                r'^amount\s*:\s*([\d,]+\.?\d*)$',
            ]
            for p in amount_patterns:
                match = re.search(p, line_clean, re.IGNORECASE)
                if match:
                    val = extract_amount(match.group(1))
                    if val and val > 1:
                        fields['total_amount'] = val
                        break

            if fields['total_amount'] is None:
                if re.search(r'^total\s*$', line_clean, re.IGNORECASE):
                    val = extract_amount(next_line)
                    if val and val > 1:
                        fields['total_amount'] = val

        # ── TAX AMOUNT ────────────────────────────────────
        if fields['tax_amount'] is None:
            tax_patterns = [
                r'service\s*tax\s*\(\d+%.*?\)\s*([\d,]+\.?\d*)',
                r'service\s*tax\s*@\s*\d+%.*?([\d,]+\.?\d*)',
                r'(?:service\s*tax|sst|gst)\s*[:\-@]?\s*\d*%?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                r'total\s*tax\s*amount\s*[:\-]?\s*([\d,]+\.?\d*)',
                r'tax\s*amount\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
            ]
            for p in tax_patterns:
                match = re.search(p, line_clean, re.IGNORECASE)
                if match:
                    val = extract_amount(match.group(1))
                    if val is not None:
                        fields['tax_amount'] = val
                        break

    # ══════════════════════════════════════════════════════
    # FALLBACKS — Generic, works for any invoice
    # ══════════════════════════════════════════════════════

    # Invoice Number fallback — generic No: XXXXX
    if fields['invoice_number'] is None:
        for pattern in [
            r'\b(SIN\d{6,})\b',
            r'No\s*[:\-]\s*([A-Z]{2,}[A-Z0-9\-\/]+)',
            r'(?:invoice|receipt|bill)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
            r'receipt\s*no\.?\s*[:\.]?\s*(\d+)',
            r'booking\s*id\s*[:\-]?\s*([A-Za-z0-9\-]+)',
        ]:
            match = re.search(pattern, ocr_text, re.IGNORECASE)
            if match:
                val = match.group(1).strip()
                if is_valid_invoice_number(val):
                    fields['invoice_number'] = val
                    break

    # Vendor fallback — find first company name not in buyer section
    if fields['vendor_name'] is None:
        all_companies = re.finditer(
            r'([\w\s&\.\-\(\)\/]+(?:sdn\.?\s*bhd\.?|berhad|enterprise|corporation|corp|ltd\.?|trading|network|logistics|broadband)[\w\s&\.\-\(\)\/\-]*)',
            ocr_text, re.IGNORECASE
        )
        for m in all_companies:
            vendor = m.group(1).strip()
            # Check surrounding context — skip if buyer
            start = max(0, m.start() - 50)
            context = ocr_text[start:m.start()].lower()
            if not any(k in context for k in ['invoice to', 'bill to', 'deliver to', 'ship to', 'sold to']):
                if len(vendor) > 5:
                    fields['vendor_name'] = clean_vendor_name(vendor)
                    break

    # Total Amount fallback
    if fields['total_amount'] is None:
        for pattern in [
            r'\btotal\s*\(\s*(?:rm|myr)\s*\)\s*[:\-]?\s*([\d,]+\.?\d*)',
            r'total\s*net\s*amount\s*(?:\(rm\))?\s*[:\-]?\s*([\d,]+\.?\d*)',
            r'total\s*(?:paid|amount|charges?|due|payable|net)\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
            r'grand\s*total\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
            r'(?:rm|myr)\s*:?\s*([\d,]+\.?\d*)\s*$',
            r'cash\s*amt\s*[:\-]?\s*([\d,]+\.?\d*)',
        ]:
            match = re.search(pattern, ocr_text, re.IGNORECASE | re.MULTILINE)
            if match:
                val = extract_amount(match.group(1))
                if val and val > 1:
                    fields['total_amount'] = val
                    break

    # Invoice Date fallback
    if fields['invoice_date'] is None:
        # Try month name format: 28-May-2025, 28 May 2025
        month_abbr = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'
        month_full = r'(?:january|february|march|april|may|june|july|august|september|october|november|december)'
        for pattern in [
            rf'(\d{{1,2}}[-\s]{month_abbr}[-\s]\d{{4}})',
            rf'(\d{{1,2}}[-\s]{month_full}[-\s]\d{{4}})',
            rf'(\d{{1,2}}\s+{month_full}\s+\d{{4}})',
            r'(\d{1,2}\/\d{1,2}\/\d{4})',
            r'(\d{1,2}-\d{1,2}-\d{4})',
            r'(\d{4}-\d{2}-\d{2})',
        ]:
            match = re.search(pattern, ocr_text, re.IGNORECASE)
            if match:
                fields['invoice_date'] = normalize_date_string(match.group(1).strip())
                break
                # Clean up wrong invoice number
    if fields['invoice_number'] and any(x in str(fields['invoice_number']) for x in ['Invoice To', 'N0016', 'invoice to', 'Bill To', 'Deliver To']):
        fields['invoice_number'] = None

    # Generic No: fallback - handle value separated by other lines
    if fields['invoice_number'] is None:
        lines_list = ocr_text.split('\n')
        for i, line in enumerate(lines_list):
            if re.search(r'^No\s*:\s*$', line.strip()):
                # Check next 3 lines for invoice number
                for j in range(1, 4):
                    if i + j < len(lines_list):
                        next_val = lines_list[i + j].strip()
                        if (next_val and
                            is_valid_invoice_number(next_val) and
                            not re.search(r'^(date|deliver|invoice|northern|unit|gurney|penang|tel|fax|attn)$',
                                         next_val, re.IGNORECASE) and
                            re.search(r'[A-Z]{2,}', next_val)):
                            fields['invoice_number'] = next_val
                            break

    # ── Gemini semantic fallback for missed fields ──
    missing = [k for k, v in fields.items() if v is None]
    if missing:
        print(f"DEBUG Regex missed invoice fields: {missing}, calling Gemini fallback")
        try:
            from helpers.gemini_extractor import gemini_extract_invoice
            g = gemini_extract_invoice(ocr_text)
            for k in missing:
                if g.get(k) is not None:
                    fields[k] = g[k]
                    print(f"DEBUG Gemini filled invoice.{k} = {g[k]}")
        except Exception as e:
            print(f"DEBUG Gemini invoice fallback error: {e}")

    print(f"DEBUG extracted fields: {fields}")
    return fields


def extract_po_fields(ocr_text):
    fields = {
        'po_number':    None,
        'vendor_name':  None,
        'po_date':      None,
        'total_amount': None,
        'currency':     'MYR',
    }

    lines = ocr_text.split('\n')

    for i, line in enumerate(lines):
        line_clean = line.strip()

        if fields['po_number'] is None:
            match = re.search(
                r'p\.?o\.?\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = match.group(1).strip()
                if len(val) > 2:
                    fields['po_number'] = val

        if fields['vendor_name'] is None:
            match = re.search(
                r'^([\w\s&\.\-\(\)]+(?:sdn\.?\s*bhd\.?|berhad|corporation|corp|ltd\.?|enterprise|trading)[\w\s&\.\-\(\)]*)',
                line_clean, re.IGNORECASE
            )
            if match:
                vendor = match.group(1).strip()
                if len(vendor) > 5:
                    fields['vendor_name'] = clean_vendor_name(vendor)

        if fields['po_date'] is None:
            match = re.search(
                r'(?:po\s*date|date|order\s*date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})',
                line_clean, re.IGNORECASE
            )
            if match:
                fields['po_date'] = normalize_date_string(match.group(1).strip())

        if fields['total_amount'] is None:
            match = re.search(
                r'(?:total|amount|grand\s*total)\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = extract_amount(match.group(1))
                if val:
                    fields['total_amount'] = val

    if fields['po_number'] is None:
        match = re.search(r'PO[-\s]?(\d+)', ocr_text, re.IGNORECASE)
        if match:
            fields['po_number'] = match.group(0).strip()

    if fields['po_date'] is None:
        match = re.search(r'(\d{1,2}\/\d{1,2}\/\d{4})', ocr_text)
        if match:
            fields['po_date'] = match.group(1).strip()

    if fields['total_amount'] is None:
        match = re.search(r'(?:rm|myr)\s*([\d,]+\.?\d*)', ocr_text, re.IGNORECASE)
        if match:
            val = extract_amount(match.group(1))
            if val:
                fields['total_amount'] = val

    # PO/GR use Gemini as PRIMARY source because these documents flip the
    # semantic role of the header company (buyer, not vendor), which regex
    # cannot reason about. Regex values are kept only as fallback if Gemini
    # fails or returns empty. Invoice extraction remains regex-primary since
    # header = seller there and regex handles it reliably.
    try:
        from helpers.gemini_extractor import gemini_extract_po
        g = gemini_extract_po(ocr_text)
        if g:  # Gemini returned something
            print(f"DEBUG Gemini primary PO result: {g}")
            # Gemini wins for these semantically-sensitive fields
            for key in ['po_number', 'vendor_name', 'po_date', 'total_amount']:
                if g.get(key) is not None:
                    old_val = fields.get(key)
                    fields[key] = g[key]
                    if old_val != g[key]:
                        print(f"DEBUG Gemini overrode PO.{key}: '{old_val}' -> '{g[key]}'")
        else:
            print("DEBUG Gemini returned empty for PO, keeping regex values")
    except Exception as e:
        print(f"DEBUG Gemini PO extraction failed: {e}, keeping regex values")

    # Regex values remain as fallback if Gemini returned None or failed

    print(f"DEBUG PO extracted fields: {fields}")
    return fields


def extract_gr_fields(ocr_text):
    fields = {
        'gr_number':    None,
        'vendor_name':  None,
        'receipt_date': None,
        'total_amount': None,
        'currency':     'MYR',
    }

    lines = ocr_text.split('\n')

    for i, line in enumerate(lines):
        line_clean = line.strip()

        if fields['gr_number'] is None:
            match = re.search(
                r'(?:gr|goods\s*receipt|delivery\s*order|do|asn)\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Za-z0-9\-\/]+)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = match.group(1).strip()
                if len(val) > 2:
                    fields['gr_number'] = val

                if fields['gr_number'] is None:
                    no_match = re.search(r'^No\s*:\s*([A-Z]{2,}[A-Z0-9]+)', line_clean, re.IGNORECASE)
                    if no_match:
                        val = no_match.group(1).strip()
                        if len(val) > 2:
                            fields['gr_number'] = val

        if fields['vendor_name'] is None:
            match = re.search(
                r'^([\w\s&\.\-\(\)]+(?:sdn\.?\s*bhd\.?|berhad|corporation|corp|ltd\.?|enterprise|trading)[\w\s&\.\-\(\)]*)',
                line_clean, re.IGNORECASE
            )
            if match:
                vendor = match.group(1).strip()
                if len(vendor) > 5:
                    fields['vendor_name'] = clean_vendor_name(vendor)

                    # Fallback for GR number
    if fields['gr_number'] is None:
        lines_list = ocr_text.split('\n')
        for i, line in enumerate(lines_list):
            if re.search(r'^No\s*:\s*$', line.strip()):
                for j in range(1, 4):
                    if i + j < len(lines_list):
                        next_val = lines_list[i + j].strip()
                        if (next_val and len(next_val) > 2 and
                            re.search(r'[A-Z]{2,}', next_val) and
                            not re.search(r'^(date|deliver|invoice|northern|unit|gurney|penang|tel|fax|attn)$',
                                         next_val, re.IGNORECASE)):
                            fields['gr_number'] = next_val
                            break

        if fields['receipt_date'] is None:
            match = re.search(
                r'(?:receipt\s*date|delivery\s*date|date\s*received|date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})',
                line_clean, re.IGNORECASE
            )
            if match:
                fields['receipt_date'] = normalize_date_string(match.group(1).strip())

        if fields['total_amount'] is None:
            match = re.search(
                r'(?:total|amount|grand\s*total)\s*[:\-]?\s*(?:rm|myr)?\s*([\d,]+\.?\d*)',
                line_clean, re.IGNORECASE
            )
            if match:
                val = extract_amount(match.group(1))
                if val:
                    fields['total_amount'] = val

    if fields['receipt_date'] is None:
        match = re.search(r'(\d{1,2}\/\d{1,2}\/\d{4})', ocr_text)
        if match:
            fields['receipt_date'] = match.group(1).strip()

    if fields['total_amount'] is None:
        match = re.search(r'(?:rm|myr)\s*([\d,]+\.?\d*)', ocr_text, re.IGNORECASE)
        if match:
            val = extract_amount(match.group(1))
            if val:
                fields['total_amount'] = val

    # PO/GR use Gemini as PRIMARY source because these documents flip the
    # semantic role of the header company (buyer, not vendor), which regex
    # cannot reason about. Regex values are kept only as fallback if Gemini
    # fails or returns empty. Invoice extraction remains regex-primary since
    # header = seller there and regex handles it reliably.
    try:
        from helpers.gemini_extractor import gemini_extract_gr
        g = gemini_extract_gr(ocr_text)
        if g:
            print(f"DEBUG Gemini primary GR result: {g}")
            for key in ['gr_number', 'vendor_name', 'receipt_date']:
                if g.get(key) is not None:
                    old_val = fields.get(key)
                    fields[key] = g[key]
                    if old_val != g[key]:
                        print(f"DEBUG Gemini overrode GR.{key}: '{old_val}' -> '{g[key]}'")
        else:
            print("DEBUG Gemini returned empty for GR, keeping regex values")
    except Exception as e:
        print(f"DEBUG Gemini GR extraction failed: {e}, keeping regex values")

    # Regex values remain as fallback if Gemini returned None or failed

    print(f"DEBUG GR extracted fields: {fields}")
    return fields