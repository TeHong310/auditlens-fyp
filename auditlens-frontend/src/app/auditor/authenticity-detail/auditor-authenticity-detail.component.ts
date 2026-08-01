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

// A location box from check.boxes, exactly as
// helpers/authenticity_check.py's _flatten_boxes/_normalize_box_to_unit_space
// already return it — coordinate_space is always "normalized_0_1" (x/y/
// width/height are fractions of the source image, 0-1), so the frontend
// never has to guess a coordinate space or convert pixels itself.
export interface EvidenceBox {
  type: string;
  label: string;
  reason: string;
  confidence: number;
  coordinate_space: string;
  x: number;
  y: number;
  width: number;
  height: number;
  localization_quality: 'exact' | 'approximate' | 'unreliable';
}

export interface HighlightBox {
  key: string;
  tagLabel: string;
  box: EvidenceBox;
  state: 'good' | 'warn' | 'bad';
  active: boolean;
}

export interface SupportingEvidenceItem {
  label: string;
  key: string | null;
}

// The 5 evidence categories that can ever carry a real, reliably-located
// bounding box (helpers/authenticity_check.py's _BOX_TYPES) — mapped to
// the short on-image tag label the task asked for, and to the stable
// `type` string check.boxes[].type uses. Visual-integrity axes (font/
// alignment/copy-paste/alteration risk) are deliberately absent: they
// are global, page-wide observations with no single physical location,
// so this page never invents a box for them.
const BOX_TAG_LABEL: Record<string, string> = {
  company_name:     'Supplier',
  supplier_address: 'Supplier',
  company_logo:     'Logo',
  stamp:             'Stamp',
  signature:          'Signature',
};

const KEY_TO_BOX_TYPE: Record<string, string> = {
  company_name:     'company_name',
  supplier_address: 'supplier_address',
  company_logo:     'supplier_logo',
  stamp:             'company_stamp',
  signature:          'signature',
};

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

  // ── Evidence highlighting — document stays clean until the auditor
  // opts in. showAllBoxes only ever matters while showHighlights is on. ──
  showHighlights = false;
  showAllBoxes = false;
  private hoveredKey: string | null = null;
  private selectedKey: string | null = null;

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

  // ── Evidence highlighting — cross-references check.boxes (already
  // normalized server-side to 0-1 fractions of the source image, see
  // helpers/authenticity_check.py's _normalize_box_to_unit_space) against
  // whichever evidence key a row/section represents. A box with
  // localization_quality "unreliable" is treated the same as no box at
  // all — never rendered — since the task is only to highlight evidence
  // with a reliably physical location, not every AI guess. ──

  private boxForType(type: string): EvidenceBox | null {
    const box = (this.check?.boxes || []).find((b: any) => b?.type === type);
    if (!box || box.localization_quality === 'unreliable') return null;
    return box as EvidenceBox;
  }

  // Public: whether this key (company_name/supplier_address/company_logo/
  // stamp/signature) has a real, reliable box to highlight. Every
  // clickable row on this page gates its interactivity on this — a row
  // with no box stays visible but plain, per the task's explicit rule.
  boxForKey(key: string | null | undefined): EvidenceBox | null {
    if (!key) return null;
    const type = KEY_TO_BOX_TYPE[key];
    return type ? this.boxForType(type) : null;
  }

  get hasAnyBoxes(): boolean {
    return Object.keys(KEY_TO_BOX_TYPE).some(key => !!this.boxForKey(key));
  }

  private evidenceKeyDetected(key: string): boolean {
    if (key === 'company_name') return !!this.supplierIdentity?.supplier_name_detected;
    if (key === 'supplier_address') return !!this.supplierIdentity?.address_detected;
    const evidence = this.check?.ai_visual_result?.document_visual_evidence?.[key];
    return !!evidence?.detected;
  }

  // Green when detected; amber when the AI could only point at a
  // plausible-but-empty location (not detected, still needs a look); red
  // only for company_name/supplier_address when identity is a genuine,
  // confirmed INCONSISTENT mismatch — a real, location-tied risk, not a
  // guess. Never derived from font/alignment/copy-paste risk, which have
  // no location at all.
  boxState(key: string): 'good' | 'warn' | 'bad' {
    if (key === 'company_name' || key === 'supplier_address') {
      if (this.identityStatus === 'INCONSISTENT') return 'bad';
      if (this.identityStatus === 'UNCERTAIN') return 'warn';
    }
    return this.evidenceKeyDetected(key) ? 'good' : 'warn';
  }

  toggleShowHighlights() {
    this.showHighlights = !this.showHighlights;
    if (!this.showHighlights) {
      this.showAllBoxes = false;
      this.selectedKey = null;
      this.hoveredKey = null;
    }
  }

  toggleShowAll() {
    this.showAllBoxes = !this.showAllBoxes;
    if (this.showAllBoxes) {
      this.selectedKey = null;
      this.hoveredKey = null;
    }
  }

  onRowEnter(key: string | null | undefined) {
    if (this.boxForKey(key)) this.hoveredKey = key as string;
  }

  onRowLeave(key: string | null | undefined) {
    if (this.hoveredKey === key) this.hoveredKey = null;
  }

  // Click/Enter/Space on a row: select it, or clear the selection if it
  // was already the active one. Also clears a same-key hover when
  // deselecting — otherwise, if the pointer is still sitting on the row
  // (the common case: you click, then re-click the same still-hovered
  // row), the lingering hover would immediately re-show the highlight
  // the click just turned off. The auditor has to move off and back onto
  // the row for hover to re-activate it, which reads as a real toggle.
  onRowActivate(key: string | null | undefined) {
    if (!this.boxForKey(key)) return;
    if (this.selectedKey === key) {
      this.selectedKey = null;
      if (this.hoveredKey === key) this.hoveredKey = null;
    } else {
      this.selectedKey = key as string;
    }
  }

  isRowActive(key: string | null | undefined): boolean {
    return !!key && (this.selectedKey === key || this.hoveredKey === key);
  }

  get visibleHighlightBoxes(): HighlightBox[] {
    if (!this.showHighlights) return [];
    const activeKey = this.hoveredKey || this.selectedKey;
    const keys = this.showAllBoxes ? Object.keys(KEY_TO_BOX_TYPE) : (activeKey ? [activeKey] : []);
    const out: HighlightBox[] = [];
    for (const key of keys) {
      const box = this.boxForKey(key);
      if (!box) continue;
      out.push({ key, tagLabel: BOX_TAG_LABEL[key], box, state: this.boxState(key), active: key === activeKey });
    }
    return out;
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
      // Ambiguous between the company_name and supplier_address boxes —
      // prefer whichever one actually has a reliable location, since
      // this one row can only point at a single region.
      const key = this.boxForKey('company_name') ? 'company_name'
        : (this.boxForKey('supplier_address') ? 'supplier_address' : null);
      items.push({ label: 'Supplier details are consistent', key });
    }
    for (const row of this.documentEvidenceRows) {
      if (items.length >= 3) break;
      if (row.status === 'yes') items.push({ label: row.label, key: row.key || null });
    }
    if (items.length < 3) {
      const signatureRow = this.documentEvidenceRows.find(r => /signature/i.test(r.label) && r.status === 'na');
      // Not-required-and-absent has nothing meaningful to point at — a
      // plain, non-interactive row rather than a highlight on empty space.
      if (signatureRow) items.push({ label: 'Signature not required', key: null });
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
