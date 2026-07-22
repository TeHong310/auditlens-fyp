// Enterprise V3 Phase 7 (FIX 3) — the ONE shared source of truth for
// interpreting an authenticity_checks row's evidence, used identically by
// the Authenticity list page's "Detected Signals" badges and the
// Authenticity Detail page's "Document Evidence" section. Both pages
// already read the SAME backend row (routes/authenticity.py's
// _SELECT_WITH_JOINS is shared by GET /authenticity and GET
// /authenticity/<id>) — the bug this file fixes was never a data
// mismatch, only two independently-written interpretation rules: the
// list page showed raw has_company_logo/has_signature booleans as a
// strict yes/no badge with no awareness of whether that signal is even
// expected for this document type, while the detail page correctly
// treated a "not required" absence (e.g. a Goods Receipt's signature) as
// neutral, not a failure. Moving that logic here and having both pages
// call it removes the duplication entirely.
//
// Does not touch the authenticity engine, Gemini/Claude vision, or the
// database — pure display-layer interpretation of fields the engine
// already computed and returned.

export type RowStatus = 'yes' | 'no' | 'warn' | 'na';

export interface EvidenceRow {
  label: string;
  status: RowStatus;
  statusLabel: string;
  reason?: string;
}

function evidenceEntry(check: any, key: string): any {
  return check?.ai_visual_result?.document_visual_evidence?.[key] || null;
}

function stampTypeLabel(check: any): string {
  const type = evidenceEntry(check, 'stamp')?.type;
  if (!type) return '';
  return type.split('_').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

function evidenceRowFromKey(check: any, key: string, label: string): EvidenceRow {
  const entry = evidenceEntry(check, key);
  const detected = !!(entry?.status === 'detected' || entry?.detected);
  const required = entry?.required !== false;
  let status: RowStatus;
  let statusLabel: string;
  if (detected) {
    status = 'yes'; statusLabel = 'Detected';
  } else if (!required) {
    status = 'na'; statusLabel = 'Not Required';
  } else {
    status = 'warn'; statusLabel = 'Needs Review';
  }
  if (key === 'stamp') {
    const t = stampTypeLabel(check);
    if (t) label = `${label} (${t})`;
  }
  return { label, status, statusLabel, reason: entry?.reason };
}

function supplierInfoRow(check: any): EvidenceRow {
  const supplierIdentity = check?.ai_visual_result?.supplier_identity || null;
  const detected = !!(supplierIdentity?.supplier_name_detected || supplierIdentity?.address_detected);
  return {
    label: 'Supplier Information Present',
    status: detected ? 'yes' : 'warn',
    statusLabel: detected ? 'Detected' : 'Needs Review',
  };
}

// PO/GR letterheads are normally the BUYER's own branding — a distinct
// supplier logo is usually absent by design, so it's a static, always-
// neutral row rather than a detection result.
const SUPPLIER_LOGO_OPTIONAL_ROW: EvidenceRow = {
  label: 'Supplier Logo',
  status: 'na',
  statusLabel: 'Optional',
};

// Single shared source of truth for "what evidence rows should this
// document type show, and is each one Detected / Needs Review / Not
// Required" — the exact same rows/logic for both the list card and the
// detail page. Returns [] when the check hasn't run the new engine yet
// (ai_visual_result is null) — the caller should fall back to whatever
// legacy display it already has for that case.
export function getAuthenticityEvidenceRows(check: any, documentType: string): EvidenceRow[] {
  if (!check?.ai_visual_result) return [];
  if (documentType === 'po') {
    return [
      evidenceRowFromKey(check, 'company_logo', 'Buyer / Issuer Letterhead Detected'),
      supplierInfoRow(check),
      SUPPLIER_LOGO_OPTIONAL_ROW,
    ];
  }
  if (documentType === 'gr') {
    return [
      evidenceRowFromKey(check, 'company_logo', 'Receiver / Buyer Letterhead Detected'),
      supplierInfoRow(check),
      evidenceRowFromKey(check, 'stamp', 'QC / Receiving Stamp Detected'),
      SUPPLIER_LOGO_OPTIONAL_ROW,
    ];
  }
  // invoice (default)
  return [
    evidenceRowFromKey(check, 'company_logo', 'Supplier Branding / Logo Detected'),
    evidenceRowFromKey(check, 'stamp', 'Received Stamp Detected'),
    evidenceRowFromKey(check, 'signature', 'Signature'),
  ];
}
