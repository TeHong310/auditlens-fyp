import { Component, OnInit, OnDestroy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ActivatedRoute, Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';
import { getAuthenticityEvidenceRows, EvidenceRow, RowStatus } from '../shared/authenticity-evidence.util';

type SignalKey = 'has_company_chop' | 'has_company_logo' | 'has_company_name' | 'has_signature';

const SIGNAL_LABELS: Record<SignalKey, string> = {
  has_company_name: 'Company Name',
  has_company_chop: 'Company Chop',
  has_signature: 'Signature',
  has_company_logo: 'Company Logo',
};

// Display labels for the 4 visual-integrity axes the AI vision engine
// already returns. "Handwritten / Altered Content" is alteration_risk's
// display name — the underlying field is unchanged, this just matches
// the auditor-facing wording this page uses everywhere else.
const RISK_LABELS: Record<string, string> = {
  copy_paste_risk:        'Copy/Paste Risk',
  font_consistency:       'Font Consistency',
  alignment_consistency:  'Alignment Consistency',
  alteration_risk:        'Handwritten / Altered Content',
};

export type OverallStatus = 'NO_CONCERNS' | 'REVIEW_REQUIRED' | 'INCONCLUSIVE' | 'HIGH_RISK';
export type IdentityStatus = 'CONSISTENT' | 'UNCERTAIN' | 'INCONSISTENT' | 'NOT CHECKED';
export type CategoryStatus = 'LOW' | 'MEDIUM' | 'HIGH' | 'PRESENT' | 'PARTIAL' | 'LIMITED' | 'NOT CHECKED';

export interface IntegrityFinding {
  label: string;
  level: 'MEDIUM' | 'HIGH';
}

// A real Google Vision OCR bounding polygon for one evidence region,
// exactly as helpers/vision_evidence_boxes.py's build_vision_evidence_boxes()
// returns it (mixed into check.boxes alongside the older AI-vision-
// estimated rectangles). source_width/source_height are the canonical
// rendered PDF page image's own pixel dimensions — the SAME image this
// page displays — so polygon coordinates need no conversion: the SVG
// overlay's viewBox is set directly from these two fields and the
// polygon points are used as-is.
export interface VisionEvidenceBox {
  id: string;
  type: string;
  label: string;
  page: number;
  source_width: number;
  source_height: number;
  polygon: { x: number; y: number }[];
  confidence: number;
  coordinate_source: string;
}

export interface SupportingEvidenceItem {
  label: string;
  key: string | null;
  // True when this item is a detected/positive finding but has no
  // reliable Google-Vision-sourced location — rendered as plain
  // "Location unavailable" text instead of a clickable row.
  locationUnavailable: boolean;
}

// One simultaneously-rendered region on the document overlay.
export interface VisibleEvidenceBox {
  key: string;
  number: number;
  box: VisionEvidenceBox;
  emphasized: boolean;
}

interface PartyRowDef {
  key: string;
  // The check.boxes[].type this row cross-references — matches exactly
  // what helpers/vision_evidence_boxes.py's build_vision_evidence_boxes()
  // builds for this document_type (see its own DOCUMENT-TYPE RULES).
  visionType: string;
  label: string;
}

// Each document type carries different real-world parties and stamps —
// an Invoice's Issuer/Supplier is a different structural role from a
// PO's Buyer/Issuer, and only a Goods Received Note carries the two
// buyer-side processing stamps. This is the ONE place that decides
// which rows this page shows per type; helpers/vision_evidence_boxes.py
// only ever builds the box types listed here for a given document_type,
// so a row here either finds its box or honestly shows nothing.
const PARTY_ROW_DEFS: Record<string, PartyRowDef[]> = {
  invoice: [
    { key: 'supplier', visionType: 'supplier_name', label: 'Invoice Issuer / Supplier' },
    { key: 'buyer', visionType: 'buyer_name', label: 'Buyer (Bill To)' },
    { key: 'buyer_stamp', visionType: 'buyer_received_stamp', label: 'Buyer Received Stamp' },
  ],
  po: [
    { key: 'buyer', visionType: 'buyer_name', label: 'PO Issuer / Buyer' },
    { key: 'supplier', visionType: 'supplier_name', label: 'Supplier' },
    { key: 'supplier_address', visionType: 'supplier_address', label: 'Supplier Address' },
  ],
  gr: [
    { key: 'buyer', visionType: 'buyer_name', label: 'GR Issuer / Receiver' },
    { key: 'supplier', visionType: 'supplier_name', label: 'Supplier' },
    { key: 'supplier_address', visionType: 'supplier_address', label: 'Supplier Address' },
    { key: 'qc_stamp', visionType: 'qc_passed_stamp', label: 'QC Passed Stamp' },
    { key: 'keyin_stamp', visionType: 'key_in_store_stamp', label: 'Key-In Store Stamp' },
  ],
};

const PARTY_SECTION_TITLE: Record<string, string> = {
  invoice: 'Invoice Parties',
  po: 'Purchase Order Parties',
  gr: 'Receiving Parties',
};

export interface PartyRow {
  key: string;
  number: number;
  label: string;
  box: VisionEvidenceBox | null;
  // True once this row has ANY positive signal — a Vision box, or (for
  // supplier/supplier_address only) Claude/Gemini's own independent
  // supplier_identity detection. Rows with no independent signal
  // (buyer, every stamp) have no other source of truth: no box means
  // honestly "not detected", never a claimed-but-unlocated positive.
  detected: boolean;
  // Detected via the independent signal but with no Vision box to
  // point at — the one case where "Location unavailable" is honest;
  // never shown for rows whose only signal IS the box itself.
  locationUnavailable: boolean;
}

@Component({
  selector: 'app-auditor-authenticity-detail',
  standalone: true,
  imports: [CommonModule, RouterLink],
  templateUrl: './auditor-authenticity-detail.component.html',
  styleUrls: ['./auditor-authenticity-detail.component.css']
})
export class AuditorAuthenticityDetailComponent implements OnInit, OnDestroy {
  documentId: number | null = null;
  documentType: string = 'invoice';

  check: any = null;
  isLoading = false;
  errorMessage = '';
  isRechecking = false;

  imageBlobUrl: string | null = null;
  // idle -> loading -> one of: 'image' (loaded), 'error' (no file / fetch
  // failed). The backend always serves an image here — a PDF's rendered
  // first page, or the original file if it's already an image.
  imageLoadState: 'idle' | 'loading' | 'image' | 'error' = 'idle';

  signalKeys: SignalKey[] = ['has_company_name', 'has_company_chop', 'has_signature', 'has_company_logo'];
  signalLabels = SIGNAL_LABELS;

  riskKeys = Object.keys(RISK_LABELS);
  riskLabels = RISK_LABELS;

  // Single "View Full Analysis" section — collapsed by default.
  fullAnalysisOpen = false;

  // ── Evidence highlighting — all reliably-located regions (supplier
  // name/address, stamp) are shown automatically as soon as the check
  // loads; no click required. Clicking a row emphasizes just that
  // region while the others stay visible at normal strength; clicking
  // it again returns every region to normal. ──
  emphasizedKey: string | null = null;

  private rawBlobUrl: string | null = null;
  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private route: ActivatedRoute,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.route.paramMap.subscribe(params => {
      const id = params.get('documentId');
      if (id) {
        this.documentId = parseInt(id, 10);
        this.documentType = this.route.snapshot.queryParamMap.get('document_type') || 'invoice';
        this.load();
      }
    });
  }

  ngOnDestroy() {
    if (this.rawBlobUrl) URL.revokeObjectURL(this.rawBlobUrl);
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  goBack() {
    this.router.navigate(['/auditor/authenticity']);
  }

  // ── Load cached check (never triggers Gemini — reads DB only) ──

  load() {
    if (!this.documentId) return;
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(
      `${this.apiUrl}/authenticity/${this.documentId}?document_type=${this.documentType}`,
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.check = res;
        this.isLoading = false;
        this.cdr.detectChanges();
        this.loadImage();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load authenticity check.';
        this.cdr.detectChanges();
      }
    });
  }

  private fileUrl(): string | null {
    if (!this.check || !this.documentId) return null;
    return `${this.apiUrl}/authenticity/${this.documentId}/image?document_type=${this.documentType}`;
  }

  loadImage() {
    const url = this.fileUrl();
    if (!url) {
      this.imageLoadState = 'error';
      this.cdr.detectChanges();
      return;
    }
    this.imageLoadState = 'loading';
    this.cdr.detectChanges();

    this.http.get(url, { headers: this.getHeaders(), responseType: 'blob' }).subscribe({
      next: (blob) => {
        if (this.rawBlobUrl) URL.revokeObjectURL(this.rawBlobUrl);
        this.rawBlobUrl = URL.createObjectURL(blob);
        this.imageBlobUrl = this.rawBlobUrl;
        this.imageLoadState = 'image';
        this.cdr.detectChanges();
      },
      error: () => {
        this.imageLoadState = 'error';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Engine dashboard helpers ──

  get hasNewResult(): boolean {
    return !!this.check?.ai_visual_result;
  }

  get supplierIdentity(): any {
    return this.check?.ai_visual_result?.supplier_identity || null;
  }

  // v9: Claude's own buyer detection (see helpers/claude_extractor.py's
  // BUYER IDENTITY section) — null for a check run before this field
  // existed, or by an engine that doesn't attempt it (Gemini/fallback).
  get buyerIdentity(): any {
    return this.check?.ai_visual_result?.buyer_identity || null;
  }

  get integrityCheck(): any {
    return this.check?.ai_visual_result?.integrity_check || null;
  }

  // Any color-coded severity word this page uses (CONSISTENT/PRESENT/LOW,
  // UNCERTAIN/PARTIAL/MEDIUM, INCONSISTENT/LIMITED/HIGH, or a neutral
  // NOT CHECKED/INCONCLUSIVE) maps through this one classifier, so the
  // summary table, the headline badge, and the risk chips all use the
  // exact same three-color language.
  statusSeverityClass(status: string): string {
    const s = (status || '').toUpperCase();
    if (['CONSISTENT', 'PRESENT', 'LOW', 'NO SIGNIFICANT CONCERNS'].includes(s)) return 'status-good';
    if (['UNCERTAIN', 'PARTIAL', 'MEDIUM', 'REVIEW REQUIRED'].includes(s)) return 'status-warn';
    if (['INCONSISTENT', 'LIMITED', 'HIGH', 'HIGH INTEGRITY RISK'].includes(s)) return 'status-bad';
    return 'status-neutral';
  }

  // Shared row status -> icon mapping used by the detail rows inside
  // "View Full Analysis". Red ("icon-no") is reserved for genuine
  // contradictions (e.g. a mismatched vendor name) — a signal that
  // simply wasn't found, but was required, reads as amber "Needs Review"
  // instead of a false-alarm red X.
  rowIconClass(status: RowStatus): string {
    return { yes: 'icon-yes', no: 'icon-no', warn: 'icon-warn', na: 'icon-na' }[status];
  }

  rowIcon(status: RowStatus): string {
    return { yes: 'ph-check', no: 'ph-x', warn: 'ph-warning', na: 'ph-minus' }[status];
  }

  // ── Category 1: Supplier Identity Consistency ──
  // Deliberately never says "Verified" — nothing on this page checks the
  // supplier against an external supplier master or company database;
  // this only cross-checks the document's own visual details against its
  // own extracted fields, so "Consistent" is the accurate word.

  supplierNameStatus(): RowStatus {
    return this.supplierIdentity?.supplier_name_detected ? 'yes' : 'warn';
  }

  supplierAddressStatus(): RowStatus {
    return this.supplierIdentity?.address_detected ? 'yes' : 'warn';
  }

  // Cross-check against the vendor_name the (separate) extraction
  // pipeline already found — a genuine mismatch here is a real
  // contradiction, so it's the one signal allowed to escalate identity
  // status all the way to INCONSISTENT.
  vendorMatchStatus(): RowStatus {
    const m = this.supplierIdentity?.vendor_name_matches_extraction;
    if (m === true) return 'yes';
    if (m === false) return 'no';
    return 'na';
  }

  vendorMatchStatusLabel(): string {
    const s = this.vendorMatchStatus();
    if (s === 'yes') return 'Matched';
    if (s === 'no') return 'Mismatch';
    return 'N/A';
  }

  get vendorMatchNote(): string {
    const supplier = this.supplierIdentity;
    if (!supplier || supplier.vendor_name_matches_extraction === null || supplier.vendor_name_matches_extraction === undefined) {
      return '';
    }
    if (supplier.vendor_name_matches_extraction) {
      return `Matches extracted vendor "${supplier.extracted_vendor_name}"`;
    }
    return `Differs from extracted vendor "${supplier.extracted_vendor_name}" — worth a second look`;
  }

  get identityStatus(): IdentityStatus {
    if (!this.hasNewResult || !this.supplierIdentity) return 'NOT CHECKED';
    if (this.vendorMatchStatus() === 'no') return 'INCONSISTENT';
    if (this.supplierIdentity.status === 'verified') return 'CONSISTENT';
    if (this.supplierIdentity.status === 'uncertain') return 'UNCERTAIN';
    return 'NOT CHECKED'; // not_found — nothing detected to compare
  }

  // ── Category 2: Visual Integrity — only Medium/High findings surface
  // prominently; Low is never shown as a "finding", only reflected in
  // the overall status. ──

  get visualIntegrityStatus(): CategoryStatus {
    if (!this.integrityCheck) return 'NOT CHECKED';
    const levels = this.riskKeys.map(k => (this.integrityCheck?.[k] || 'low').toLowerCase());
    if (levels.includes('high')) return 'HIGH';
    if (levels.includes('medium')) return 'MEDIUM';
    return 'LOW';
  }

  get visualIntegrityFindings(): IntegrityFinding[] {
    if (!this.integrityCheck) return [];
    const out: IntegrityFinding[] = [];
    for (const key of this.riskKeys) {
      const level = (this.integrityCheck[key] || 'low').toLowerCase();
      if (level === 'medium' || level === 'high') {
        out.push({ label: this.riskLabels[key], level: level.toUpperCase() as 'MEDIUM' | 'HIGH' });
      }
    }
    return out;
  }

  // ── Category 3: Document Evidence (document-type-specific wording) ──
  // Delegates to the shared util also used by the Authenticity list
  // page's "Detected Signals" badges, so both pages can never disagree
  // about the same document again. A buyer QC/receiving stamp is already
  // labelled as processing evidence there ("QC / Receiving Stamp
  // Detected"), never as supplier identity verification.

  get documentEvidenceRows(): EvidenceRow[] {
    return getAuthenticityEvidenceRows(this.check, this.documentType);
  }

  get documentEvidenceStatus(): CategoryStatus {
    const rows = this.documentEvidenceRows;
    const required = rows.filter(r => r.status !== 'na');
    if (!required.length) return 'NOT CHECKED';
    const detected = required.filter(r => r.status === 'yes').length;
    if (detected === required.length) return 'PRESENT';
    if (detected === 0) return 'LIMITED';
    return 'PARTIAL';
  }

  // ── Evidence highlighting — document-type-aware. Which parties/
  // stamps can even be looked for is decided once, per document_type,
  // by PARTY_ROW_DEFS (mirroring exactly what helpers/
  // vision_evidence_boxes.py builds for that type); this then cross-
  // references check.boxes for a v7 Google-Vision-sourced polygon
  // matching each row's vision type. Per the task's validation rules:
  // only a box with coordinate_source "google_vision", a real 4-point
  // polygon, valid positive source dimensions, and a page number
  // matching the single page this preview ever shows (page 1) is
  // trusted. ──

  get partyRowDefs(): PartyRowDef[] {
    return PARTY_ROW_DEFS[this.documentType] || PARTY_ROW_DEFS['invoice'];
  }

  get partySectionTitle(): string {
    return PARTY_SECTION_TITLE[this.documentType] || 'Document Parties';
  }

  private partyVisionBox(visionType: string): VisionEvidenceBox | null {
    const box = (this.check?.boxes || []).find((b: any) =>
      b?.type === visionType &&
      b?.coordinate_source === 'google_vision' &&
      b?.page === 1 &&
      typeof b?.source_width === 'number' && b.source_width > 0 &&
      typeof b?.source_height === 'number' && b.source_height > 0 &&
      Array.isArray(b?.polygon) && b.polygon.length === 4 &&
      b.polygon.every((p: any) =>
        typeof p?.x === 'number' && typeof p?.y === 'number' &&
        p.x >= 0 && p.x <= b.source_width && p.y >= 0 && p.y <= b.source_height
      ) &&
      typeof b?.confidence === 'number' && b.confidence >= 0.5
    );
    return (box as VisionEvidenceBox) || null;
  }

  // One row per party/stamp this document_type can carry. Numbered by
  // position — stable for a given type regardless of which rows ended
  // up with a box this time, so the on-image marker and the row it
  // corresponds to always agree.
  get partyRows(): PartyRow[] {
    return this.partyRowDefs.map((def, i) => {
      const box = this.partyVisionBox(def.visionType);
      // supplier/supplier_address/buyer have an independent (non-box)
      // signal — Claude/Gemini's own supplier_identity/buyer_identity
      // detection; for Invoice, buyer_stamp does too (Claude's own
      // document_visual_evidence.stamp detection). qc_stamp/keyin_stamp
      // (GR) have no other source of truth in this app: a missing box
      // for those honestly means "not detected", never "detected but
      // unlocated".
      let independentDetected: boolean | null = null;
      if (def.key === 'supplier') independentDetected = this.supplierNameStatus() === 'yes';
      else if (def.key === 'supplier_address') independentDetected = this.supplierAddressStatus() === 'yes';
      else if (def.key === 'buyer') independentDetected = this.buyerIdentity?.status === 'detected';
      else if (def.key === 'buyer_stamp') {
        independentDetected = this.check?.ai_visual_result?.document_visual_evidence?.stamp?.status === 'detected';
      }
      const detected = !!box || independentDetected === true;
      // Invoice only: show the actual anchor Claude found ("Accounts
      // Payable", "Ship To", etc.) instead of the generic default label,
      // when one is known — the anchor used varies per document.
      let label = def.label;
      if (def.key === 'buyer' && this.buyerIdentity?.anchor) {
        label = `Buyer (${this.buyerIdentity.anchor})`;
      }
      return {
        key: def.key,
        number: i + 1,
        label,
        box,
        detected,
        locationUnavailable: detected && !box,
      };
    });
  }

  visionBoxForKey(key: string | null | undefined): VisionEvidenceBox | null {
    if (!key) return null;
    return this.partyRows.find(r => r.key === key)?.box || null;
  }

  get hasLocatableEvidence(): boolean {
    return this.partyRows.some(r => !!r.box);
  }

  // Every party/stamp that currently has a real box — rendered
  // simultaneously and automatically, no click required. Each row key
  // maps to exactly one vision type, so this list can never contain a
  // duplicate region for the same evidence.
  get visibleEvidenceBoxes(): VisibleEvidenceBox[] {
    const out: VisibleEvidenceBox[] = [];
    for (const row of this.partyRows) {
      if (!row.box) continue;
      out.push({ key: row.key, number: row.number, box: row.box, emphasized: row.key === this.emphasizedKey });
    }
    return out;
  }

  // All visible boxes share the same canonical image, so any one of
  // them carries the correct source dimensions for the shared viewBox.
  get overlayViewBox(): string | null {
    const first = this.visibleEvidenceBoxes[0];
    return first ? `0 0 ${first.box.source_width} ${first.box.source_height}` : null;
  }

  // Click/Enter/Space on a row: emphasize it, or return to normal if it
  // was already emphasized. A row with no reliable box is inert.
  onRowActivate(key: string | null | undefined) {
    if (!this.visionBoxForKey(key)) return;
    this.emphasizedKey = this.emphasizedKey === key ? null : (key as string);
  }

  isRowActive(key: string | null | undefined): boolean {
    return !!key && this.emphasizedKey === key;
  }

  polygonPoints(box: VisionEvidenceBox): string {
    return box.polygon.map(p => `${p.x},${p.y}`).join(' ');
  }

  // Small numbered marker sits just outside the polygon's top-left
  // corner, clamped so it never renders off the top/left edge of the
  // source image.
  markerPosition(box: VisionEvidenceBox): { x: number; y: number } {
    const xs = box.polygon.map(p => p.x);
    const ys = box.polygon.map(p => p.y);
    return { x: Math.max(0, Math.min(...xs) - 4), y: Math.max(0, Math.min(...ys) - 4) };
  }

  // ── Top summary: one overall qualitative status, never a numeric
  // score and never a guaranteed "authentic" claim. Derived directly
  // from the 3 category signals above — never from transaction/matching
  // data, which this page doesn't assess at all. ──

  get overallStatus(): OverallStatus {
    if (!this.hasNewResult) return 'INCONCLUSIVE';
    const identity = this.identityStatus;
    const integrity = this.visualIntegrityStatus;
    if (identity === 'INCONSISTENT' || integrity === 'HIGH') return 'HIGH_RISK';
    if (identity === 'UNCERTAIN' || integrity === 'MEDIUM') return 'REVIEW_REQUIRED';
    if (identity === 'NOT CHECKED' && this.documentEvidenceStatus === 'LIMITED') return 'INCONCLUSIVE';
    return 'NO_CONCERNS';
  }

  get overallStatusLabel(): string {
    const labels: Record<OverallStatus, string> = {
      NO_CONCERNS:     'NO SIGNIFICANT CONCERNS',
      REVIEW_REQUIRED: 'REVIEW REQUIRED',
      INCONCLUSIVE:    'INCONCLUSIVE',
      HIGH_RISK:       'HIGH INTEGRITY RISK',
    };
    return labels[this.overallStatus];
  }

  get summaryExplanation(): string {
    if (!this.hasNewResult) {
      return 'This document has not yet been assessed by the authenticity engine. Run Re-check Analysis to generate an assessment.';
    }
    const parts: string[] = [];
    switch (this.identityStatus) {
      case 'CONSISTENT':   parts.push('Supplier identity is consistent.'); break;
      case 'UNCERTAIN':    parts.push('Supplier identity could not be fully confirmed.'); break;
      case 'INCONSISTENT': parts.push('Supplier identity shows a mismatch that needs review.'); break;
      default:              parts.push('Supplier identity details were not detected on this document.');
    }
    const flagged = this.visualIntegrityFindings.length;
    if (flagged > 0) {
      const noun = flagged > 1 ? 'observations' : 'observation';
      const verb = flagged > 1 ? 'require' : 'requires';
      parts.push(`${flagged} visual-integrity ${noun} ${verb} auditor review.`);
    } else {
      parts.push('No visual-integrity concerns were found.');
    }
    return parts.join(' ');
  }

  get hasVisualIntegrityConcerns(): boolean {
    return this.visualIntegrityFindings.length > 0;
  }

  // ── Supporting Evidence — up to 3 concise, deduplicated positives,
  // built concept-by-concept from the structured fields (rather than
  // merging free-text reason strings) so nothing repeats under
  // different wording. ──

  get supportingEvidenceItems(): SupportingEvidenceItem[] {
    const items: SupportingEvidenceItem[] = [];
    if (this.identityStatus === 'CONSISTENT') {
      // Ambiguous between the supplier and supplier_address boxes —
      // prefer whichever one actually has a reliable location, since
      // this one row can only point at a single region.
      const nameBox = this.visionBoxForKey('supplier');
      const addrBox = this.visionBoxForKey('supplier_address');
      const key = nameBox ? 'supplier' : (addrBox ? 'supplier_address' : null);
      items.push({ label: 'Supplier details are consistent', key, locationUnavailable: !nameBox && !addrBox });
    }
    for (const row of this.documentEvidenceRows) {
      if (items.length >= 3) break;
      if (row.status === 'yes') {
        items.push({ label: row.label, key: row.key || null, locationUnavailable: !this.visionBoxForKey(row.key) });
      }
    }
    if (items.length < 3) {
      const signatureRow = this.documentEvidenceRows.find(r => /signature/i.test(r.label) && r.status === 'na');
      // Not-required-and-absent has nothing meaningful to point at — a
      // plain, non-interactive row rather than a highlight on empty space.
      if (signatureRow) items.push({ label: 'Signature not required', key: null, locationUnavailable: false });
    }
    return items.slice(0, 3);
  }

  // trackBy functions for every *ngFor sourced from a getter above —
  // those getters build a fresh array (and fresh objects) on every
  // change-detection run, so without trackBy, Angular's default
  // identity-based diffing sees "all new items" on every single hover/
  // click and tears down + recreates the DOM nodes, which in turn
  // retriggers mouseenter on the replacement nodes. Tracking by a
  // stable string key stops that churn.
  trackByLabel(_index: number, item: { label: string }): string {
    return item.label;
  }

  trackByKey(_index: number, item: { key: string }): string {
    return item.key;
  }

  // ── Re-check: the only action on this page that calls Gemini/Claude ──

  recheck() {
    if (!this.documentId || this.isRechecking) return;
    this.isRechecking = true;
    this.errorMessage = '';
    this.cdr.detectChanges();

    this.http.post<any>(
      `${this.apiUrl}/authenticity/${this.documentId}/recheck?document_type=${this.documentType}`,
      {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.check = res;
        this.isRechecking = false;
        // A recheck regenerates every region from scratch (see
        // run_authenticity_check's DB write — the whole `boxes` column
        // is replaced, never merged) — clear any stale on-image
        // emphasis from before the recheck rather than pointing at a
        // key whose region may no longer exist.
        this.emphasizedKey = null;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isRechecking = false;
        this.errorMessage = err.error?.error || 'Re-check failed.';
        this.cdr.detectChanges();
      }
    });
  }

  toggleFullAnalysis() {
    this.fullAnalysisOpen = !this.fullAnalysisOpen;
  }

  // ── Display helpers ──

  docTypeLabel(type: string): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'PO';
    if (type === 'gr') return 'GR';
    return type || 'Unknown';
  }

  uploadSourceIcon(source: string): string {
    if (source === 'phone_photo') return 'ph-device-mobile-camera';
    if (source === 'scanned') return 'ph-printer';
    if (source === 'digital_native') return 'ph-desktop';
    if (source === 'webcam') return 'ph-webcam';
    return 'ph-question';
  }

  uploadSourceLabel(source: string): string {
    if (source === 'phone_photo') return 'Phone Photo';
    if (source === 'scanned') return 'Scanned';
    if (source === 'digital_native') return 'Digital Native';
    if (source === 'webcam') return 'Webcam';
    return 'Unknown';
  }
}
