import { Component, OnInit, OnDestroy, ChangeDetectorRef, ViewChild, ElementRef, HostListener } from '@angular/core';
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
// the auditor-facing wording this page uses everywhere else. These are
// GLOBAL, page-wide checks — never given a location box, per the v10
// spec's explicit "keep these separate" rule.
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
// returns it. source_width/source_height are the canonical rendered PDF
// page image's own pixel dimensions — the SAME image this page
// displays — so polygon coordinates need no conversion: the SVG
// overlay's viewBox is set directly from these two fields and the
// polygon points are used as-is. Also used (constructed client-side) to
// represent an auditor correction converted into the same pixel space
// for rendering — see regionToBox().
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

// v10: a saved row from authenticity_evidence_corrections, exactly as
// helpers/evidence_corrections.py's get_corrections_for() returns it.
// x/y/width/height are null when source is 'auditor_deleted' (the
// auditor explicitly removed a location — distinct from no correction
// ever having been made at all).
export interface EvidenceCorrection {
  correction_id: number;
  evidence_type: string;
  page: number;
  x: number | null;
  y: number | null;
  width: number | null;
  height: number | null;
  source: 'auditor_added' | 'auditor_corrected' | 'auditor_deleted';
  original_ai_box: any;
  corrected_by: number;
  corrected_at: string;
}

// Normalized 0-1 rectangle — the shape stored server-side and the shape
// all edit-mode dragging/resizing math is converted to/from at the
// save boundary. Everything ON SCREEN (rendering, drag, resize, draw)
// happens in pixel/viewBox space instead (see EditableRect) so it can
// share the SVG's existing viewBox="0 0 sourceWidth sourceHeight"
// architecture without a second coordinate system for interaction.
export interface NormalizedRegion {
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

// A rectangle in the SAME pixel/viewBox space as VisionEvidenceBox's
// polygon (0..source_width, 0..source_height) — the space every drag/
// resize/draw interaction is computed in.
interface EditableRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

type EditAction = 'correct' | 'add' | 'delete' | 'reset';

interface StagedEdit {
  evidenceType: string;
  action: EditAction;
  rect: EditableRect | null; // null for 'delete' / 'reset'
}

export type EvidenceStatus =
  | 'AI_DETECTED' | 'AUDITOR_CORRECTED' | 'AUDITOR_ADDED'
  | 'NEEDS_LOCATION' | 'NOT_DETECTED' | 'NOT_REQUIRED';

const STATUS_LABEL: Record<EvidenceStatus, string> = {
  AI_DETECTED:        'AI Detected',
  AUDITOR_CORRECTED:  'Auditor Corrected',
  AUDITOR_ADDED:       'Auditor Added',
  NEEDS_LOCATION:      'Needs Location',
  NOT_DETECTED:        'Not Detected',
  NOT_REQUIRED:        'Not Required',
};

// Rows that carry a real, locatable box use the same green "detected"
// check; every other status gets its own non-check icon — the v10 spec
// is explicit that a green check must never appear for evidence with no
// location.
const STATUS_HAS_LOCATION: Record<EvidenceStatus, boolean> = {
  AI_DETECTED: true, AUDITOR_CORRECTED: true, AUDITOR_ADDED: true,
  NEEDS_LOCATION: false, NOT_DETECTED: false, NOT_REQUIRED: false,
};

const STATUS_ICON_CLASS: Record<EvidenceStatus, string> = {
  AI_DETECTED: 'icon-yes', AUDITOR_CORRECTED: 'icon-yes', AUDITOR_ADDED: 'icon-yes',
  NEEDS_LOCATION: 'icon-warn', NOT_DETECTED: 'icon-no', NOT_REQUIRED: 'icon-na',
};

const STATUS_ICON: Record<EvidenceStatus, string> = {
  AI_DETECTED: 'ph-check', AUDITOR_CORRECTED: 'ph-check', AUDITOR_ADDED: 'ph-check',
  NEEDS_LOCATION: 'ph-map-pin-line', NOT_DETECTED: 'ph-x', NOT_REQUIRED: 'ph-minus',
};

export interface SupportingEvidenceItem {
  label: string;
  key: string | null;
  locationUnavailable: boolean;
}

// One simultaneously-rendered region on the document overlay.
export interface VisibleEvidenceBox {
  key: string;
  number: number;
  box: VisionEvidenceBox;
  emphasized: boolean;
  status: EvidenceStatus;
}

interface PartyRowDef {
  key: string; // == the evidence_type this row cross-references, both in
               // check.boxes[].type (AI) and authenticity_evidence_
               // corrections.evidence_type (auditor) — ONE shared
               // vocabulary, no separate UI alias.
  label: string;
  // Optional evidence (handwritten notes) defaults to "Not Required"
  // rather than "Not Detected" when nothing has been found — there's
  // no automatic detector for it at all (see helpers/
  // vision_evidence_boxes.py's own docstring on why), so "not present"
  // is the expected common case, not a gap.
  optional?: boolean;
}

// Each document type carries different real-world parties and stamps.
// This is the ONE place that decides which rows this page shows per
// type, and the ONE place that must match helpers/evidence_corrections.
// py's VALID_EVIDENCE_TYPES_BY_DOC_TYPE (kept in sync manually — see
// that module's own docstring on why the two lists aren't imported
// from one shared source).
const PARTY_ROW_DEFS: Record<string, PartyRowDef[]> = {
  invoice: [
    { key: 'supplier_name', label: 'Invoice Issuer / Supplier' },
    { key: 'buyer_name', label: 'Buyer' },
    { key: 'buyer_received_stamp', label: 'Buyer Received Stamp' },
  ],
  po: [
    { key: 'buyer_name', label: 'PO Issuer / Buyer' },
    { key: 'supplier_name', label: 'Supplier' },
    { key: 'supplier_address', label: 'Supplier Address' },
  ],
  gr: [
    { key: 'buyer_name', label: 'Receiver / Buyer' },
    { key: 'supplier_name', label: 'Supplier' },
    { key: 'supplier_address', label: 'Supplier Address' },
    { key: 'qc_passed_stamp', label: 'QC Passed Stamp' },
    { key: 'key_in_store_stamp', label: 'Key-In Store Stamp' },
    { key: 'handwritten_notes', label: 'Handwritten Processing Notes', optional: true },
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
  box: VisionEvidenceBox | null;     // the DISPLAYED region (staged edit > saved correction > AI)
  hasAiBox: boolean;                  // whether AI ever located this one, regardless of correction
  canResetToAi: boolean;              // true when there's a correction/staged edit AND an AI box to revert to
  status: EvidenceStatus;
  statusLabel: string;
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
  // v10: shown briefly after a recheck when auditor corrections existed
  // and were retained — cleared on the next user interaction with the
  // page rather than a timer, so it's never missed.
  recheckMessage: string | null = null;

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

  // ── Evidence highlighting (view mode) — all reliably-located regions
  // are shown automatically as soon as the check loads; no click
  // required. Clicking a row emphasizes just that region while the
  // others stay visible at normal strength; clicking it again returns
  // every region to normal. ──
  emphasizedKey: string | null = null;

  // ── Edit mode (v10 Human-in-the-Loop editing) ──
  editMode = false;
  stagedEdits: Record<string, StagedEdit> = {};
  selectedEditKey: string | null = null;
  drawingNewBox = false;
  pendingNewRect: EditableRect | null = null;
  typePickerOpen = false;
  isSavingEdits = false;

  // The canonical image's own true pixel dimensions, captured once the
  // <img> finishes loading — used for the SVG viewBox and every
  // coordinate conversion, so drawing/editing works even for a document
  // with ZERO existing AI boxes to derive dimensions from otherwise.
  sourceWidth = 0;
  sourceHeight = 0;

  readonly editHandles: ReadonlyArray<'nw' | 'ne' | 'sw' | 'se'> = ['nw', 'ne', 'sw', 'se'];

  @ViewChild('svgEl') svgRef?: ElementRef<SVGSVGElement>;

  private dragState: {
    kind: 'move' | 'resize' | 'draw';
    key: string | null;
    handle?: 'nw' | 'ne' | 'sw' | 'se';
    startClientX: number;
    startClientY: number;
    startRect: EditableRect;
  } | null = null;

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

  // The canonical image's true pixel size — the one source of truth for
  // every coordinate conversion on this page (SVG viewBox, drag/resize/
  // draw math, save-time normalization). Captured from the loaded <img>
  // itself so it's available even when the document has no AI boxes at
  // all yet to derive dimensions from.
  onImageLoad(img: HTMLImageElement) {
    this.sourceWidth = img.naturalWidth;
    this.sourceHeight = img.naturalHeight;
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
  // the overall status. Global, page-wide checks — never a location. ──

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
  // page's "Detected Signals" badges — untouched, so the list page is
  // completely unaffected. v10 dedup: 'stamp' and the combined
  // "Supplier Information Present" row are filtered out HERE ONLY (not
  // in the shared util) because they now duplicate the document-type-
  // aware Parties panel above (Buyer Received Stamp / QC Passed Stamp /
  // Key-In Store Stamp / Supplier) — company_logo stays, since a logo
  // graphic is a genuinely distinct signal from a party's name/address.

  get documentEvidenceRows(): EvidenceRow[] {
    const rows = getAuthenticityEvidenceRows(this.check, this.documentType);
    return rows.filter(r => r.key !== 'stamp' && r.label !== 'Supplier Information Present');
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

  // ══════════════════════════════════════════════════════════════════
  // ── Parties & Stamps — document-type-aware, Human-in-the-Loop ──
  // ══════════════════════════════════════════════════════════════════

  get partyRowDefs(): PartyRowDef[] {
    return PARTY_ROW_DEFS[this.documentType] || PARTY_ROW_DEFS['invoice'];
  }

  get partySectionTitle(): string {
    return PARTY_SECTION_TITLE[this.documentType] || 'Document Parties';
  }

  private aiBoxFor(evidenceType: string): VisionEvidenceBox | null {
    const box = (this.check?.boxes || []).find((b: any) =>
      b?.type === evidenceType &&
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

  private correctionFor(evidenceType: string): EvidenceCorrection | null {
    return (this.check?.evidence_corrections || []).find((c: EvidenceCorrection) => c.evidence_type === evidenceType) || null;
  }

  // Converts a normalized 0-1 region (a saved correction, or a staged
  // edit rect expressed in pixel space) into the same VisionEvidenceBox
  // shape the AI boxes use, so one render path handles both.
  private rectToBox(evidenceType: string, label: string, rect: EditableRect): VisionEvidenceBox {
    const { x, y, width, height } = rect;
    return {
      id: `${evidenceType}-auditor`,
      type: evidenceType,
      label,
      page: 1,
      source_width: this.sourceWidth,
      source_height: this.sourceHeight,
      polygon: [
        { x, y }, { x: x + width, y }, { x: x + width, y: y + height }, { x, y: y + height },
      ],
      confidence: 1,
      coordinate_source: 'auditor',
    };
  }

  private correctionToRect(c: EvidenceCorrection): EditableRect | null {
    if (c.x === null || c.y === null || c.width === null || c.height === null) return null;
    return {
      x: c.x * this.sourceWidth, y: c.y * this.sourceHeight,
      width: c.width * this.sourceWidth, height: c.height * this.sourceHeight,
    };
  }

  private boxToRect(box: VisionEvidenceBox): EditableRect {
    const xs = box.polygon.map(p => p.x);
    const ys = box.polygon.map(p => p.y);
    const x = Math.min(...xs), y = Math.min(...ys);
    return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
  }

  private independentSignalFor(key: string): boolean | null {
    if (key === 'supplier_name') return this.supplierNameStatus() === 'yes';
    if (key === 'supplier_address') return this.supplierAddressStatus() === 'yes';
    if (key === 'buyer_name') return this.buyerIdentity ? this.buyerIdentity.status === 'detected' : null;
    if (key === 'buyer_received_stamp') {
      const stamp = this.check?.ai_visual_result?.document_visual_evidence?.stamp;
      return stamp ? stamp.status === 'detected' : null;
    }
    return null; // qc_passed_stamp / key_in_store_stamp / handwritten_notes — box-only signal
  }

  // One row per party/stamp this document_type can carry, numbered by
  // position (stable regardless of which rows have a box right now, so
  // the on-image marker and its row always agree). Priority for the
  // DISPLAYED region: a staged edit (edit mode only) > a saved auditor
  // correction > the AI's own region.
  get partyRows(): PartyRow[] {
    return this.partyRowDefs.map((def, i) => {
      const aiBox = this.aiBoxFor(def.key);
      const correction = this.correctionFor(def.key);
      const staged = this.stagedEdits[def.key];
      const independent = this.independentSignalFor(def.key);

      let box: VisionEvidenceBox | null = null;
      let status: EvidenceStatus;

      if (staged) {
        if (staged.action === 'add' || staged.action === 'correct') {
          box = this.rectToBox(def.key, def.label, staged.rect!);
          status = staged.action === 'add' ? 'AUDITOR_ADDED' : 'AUDITOR_CORRECTED';
        } else {
          box = null; // delete / reset, staged
          status = independent === true ? 'NEEDS_LOCATION' : (def.optional ? 'NOT_REQUIRED' : 'NOT_DETECTED');
        }
      } else if (correction && correction.source !== 'auditor_deleted') {
        const rect = this.correctionToRect(correction);
        box = rect ? this.rectToBox(def.key, def.label, rect) : null;
        status = correction.source === 'auditor_added' ? 'AUDITOR_ADDED' : 'AUDITOR_CORRECTED';
      } else if (correction && correction.source === 'auditor_deleted') {
        box = null;
        status = independent === true ? 'NEEDS_LOCATION' : (def.optional ? 'NOT_REQUIRED' : 'NOT_DETECTED');
      } else if (aiBox) {
        box = aiBox;
        status = 'AI_DETECTED';
      } else {
        box = null;
        status = independent === true ? 'NEEDS_LOCATION' : (def.optional ? 'NOT_REQUIRED' : 'NOT_DETECTED');
      }

      // Invoice only: show the actual anchor Claude found ("Accounts
      // Payable", "Ship To", etc.) instead of the generic default label.
      let label = def.label;
      if (def.key === 'buyer_name' && this.buyerIdentity?.anchor) {
        label = `${def.label} (${this.buyerIdentity.anchor})`;
      }

      return {
        key: def.key,
        number: i + 1,
        label,
        box,
        hasAiBox: !!aiBox,
        // "Reset to Latest AI Result" only makes sense when there IS an
        // AI result to revert to, and something (a staged edit or a
        // saved correction) is currently overriding it.
        canResetToAi: !!aiBox && (!!staged || !!correction),
        status,
        statusLabel: STATUS_LABEL[status],
      };
    });
  }

  statusIconClass(status: EvidenceStatus): string { return STATUS_ICON_CLASS[status]; }
  statusIcon(status: EvidenceStatus): string { return STATUS_ICON[status]; }
  statusHasLocation(status: EvidenceStatus): boolean { return STATUS_HAS_LOCATION[status]; }

  visionBoxForKey(key: string | null | undefined): VisionEvidenceBox | null {
    if (!key) return null;
    return this.partyRows.find(r => r.key === key)?.box || null;
  }

  get hasLocatableEvidence(): boolean {
    return this.partyRows.some(r => !!r.box);
  }

  // Every party/stamp that currently has a real box — rendered
  // simultaneously and automatically, no click required. Each row key
  // maps to exactly one evidence type, so this list can never contain a
  // duplicate region for the same evidence.
  get visibleEvidenceBoxes(): VisibleEvidenceBox[] {
    const out: VisibleEvidenceBox[] = [];
    for (const row of this.partyRows) {
      if (!row.box) continue;
      out.push({
        key: row.key, number: row.number, box: row.box,
        emphasized: this.editMode ? row.key === this.selectedEditKey : row.key === this.emphasizedKey,
        status: row.status,
      });
    }
    return out;
  }

  // All visible boxes share the same canonical image, so the captured
  // source dimensions are the single source of truth for the viewBox —
  // NOT derived from any one box, so the overlay (and the ability to
  // draw a first box) exists even with zero current boxes.
  get overlayViewBox(): string | null {
    if (!this.sourceWidth || !this.sourceHeight) return null;
    return `0 0 ${this.sourceWidth} ${this.sourceHeight}`;
  }

  // Click/Enter/Space on a row: emphasize it, or return to normal if it
  // was already emphasized. A row with no reliable box is inert. Inert
  // while in edit mode too — editing uses selectForEdit() instead.
  onRowActivate(key: string | null | undefined) {
    if (this.editMode) return;
    if (!this.visionBoxForKey(key)) return;
    this.emphasizedKey = this.emphasizedKey === key ? null : (key as string);
  }

  isRowActive(key: string | null | undefined): boolean {
    if (!key) return false;
    return this.editMode ? this.selectedEditKey === key : this.emphasizedKey === key;
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

  // ════════════════════ Edit mode: interaction ════════════════════

  toggleEditMode() {
    if (this.editMode) {
      this.cancelEdits();
    } else {
      this.editMode = true;
      this.emphasizedKey = null;
    }
  }

  cancelEdits() {
    this.editMode = false;
    this.stagedEdits = {};
    this.selectedEditKey = null;
    this.drawingNewBox = false;
    this.pendingNewRect = null;
    this.typePickerOpen = false;
    this.dragState = null;
  }

  get hasStagedEdits(): boolean {
    return Object.keys(this.stagedEdits).length > 0;
  }

  selectForEdit(key: string) {
    if (!this.editMode) return;
    this.selectedEditKey = this.selectedEditKey === key ? null : key;
    this.drawingNewBox = false;
  }

  startDrawingNewBox() {
    this.drawingNewBox = true;
    this.selectedEditKey = null;
    this.typePickerOpen = false;
    this.pendingNewRect = null;
  }

  deleteSelected() {
    const key = this.selectedEditKey;
    if (!key) return;
    const hadPriorLocation = this.partyRows.find(r => r.key === key)?.hasAiBox ||
      !!this.correctionFor(key);
    if (!hadPriorLocation) {
      // A fresh, never-saved staged add with nothing behind it — just
      // drop the staged edit entirely, nothing to tell the server.
      delete this.stagedEdits[key];
    } else {
      this.stagedEdits[key] = { evidenceType: key, action: 'delete', rect: null };
    }
    this.selectedEditKey = null;
  }

  resetToAi(key: string) {
    this.stagedEdits[key] = { evidenceType: key, action: 'reset', rect: null };
    if (this.selectedEditKey === key) this.selectedEditKey = null;
  }

  private clientToViewBox(clientX: number, clientY: number): { x: number; y: number } {
    const svgEl = this.svgRef?.nativeElement;
    if (!svgEl) return { x: 0, y: 0 };
    const rect = svgEl.getBoundingClientRect();
    const fracX = rect.width ? (clientX - rect.left) / rect.width : 0;
    const fracY = rect.height ? (clientY - rect.top) / rect.height : 0;
    return { x: fracX * this.sourceWidth, y: fracY * this.sourceHeight };
  }

  onBoxPointerDown(event: PointerEvent, key: string) {
    if (!this.editMode) return;
    event.stopPropagation();
    event.preventDefault();
    this.selectedEditKey = key;
    this.drawingNewBox = false;
    const row = this.partyRows.find(r => r.key === key);
    if (!row?.box) return;
    this.dragState = {
      kind: 'move', key,
      startClientX: event.clientX, startClientY: event.clientY,
      startRect: this.boxToRect(row.box),
    };
  }

  onHandlePointerDown(event: PointerEvent, key: string, handle: 'nw' | 'ne' | 'sw' | 'se') {
    if (!this.editMode) return;
    event.stopPropagation();
    event.preventDefault();
    const row = this.partyRows.find(r => r.key === key);
    if (!row?.box) return;
    this.dragState = {
      kind: 'resize', key, handle,
      startClientX: event.clientX, startClientY: event.clientY,
      startRect: this.boxToRect(row.box),
    };
  }

  onCanvasPointerDown(event: PointerEvent) {
    if (!this.editMode || !this.drawingNewBox) return;
    event.preventDefault();
    const start = this.clientToViewBox(event.clientX, event.clientY);
    this.dragState = {
      kind: 'draw', key: null,
      startClientX: event.clientX, startClientY: event.clientY,
      startRect: { x: start.x, y: start.y, width: 0, height: 0 },
    };
    this.pendingNewRect = { ...this.dragState.startRect };
  }

  // Bound at the window level (not on the SVG element) so a fast drag
  // that briefly moves the pointer outside the SVG's rendered bounds
  // never drops the in-progress move/resize/draw — Angular template
  // event bindings only fire for events whose target is within that
  // element's subtree, which a fast pointermove can momentarily escape.
  @HostListener('window:pointermove', ['$event'])
  onSvgPointerMove(event: PointerEvent) {
    if (!this.dragState) return;
    const svgEl = this.svgRef?.nativeElement;
    const rect = svgEl?.getBoundingClientRect();
    const scaleX = rect && rect.width ? this.sourceWidth / rect.width : 1;
    const scaleY = rect && rect.height ? this.sourceHeight / rect.height : 1;
    const dx = (event.clientX - this.dragState.startClientX) * scaleX;
    const dy = (event.clientY - this.dragState.startClientY) * scaleY;

    if (this.dragState.kind === 'move' && this.dragState.key) {
      const next = this.clampRect({
        x: this.dragState.startRect.x + dx, y: this.dragState.startRect.y + dy,
        width: this.dragState.startRect.width, height: this.dragState.startRect.height,
      });
      this.stagedEdits[this.dragState.key] = { evidenceType: this.dragState.key, action: this.actionFor(this.dragState.key), rect: next };
    } else if (this.dragState.kind === 'resize' && this.dragState.key && this.dragState.handle) {
      const next = this.clampRect(this.applyResize(this.dragState.startRect, this.dragState.handle, dx, dy));
      this.stagedEdits[this.dragState.key] = { evidenceType: this.dragState.key, action: this.actionFor(this.dragState.key), rect: next };
    } else if (this.dragState.kind === 'draw') {
      const start = this.dragState.startRect;
      const curX = start.x + dx, curY = start.y + dy;
      this.pendingNewRect = this.clampRect({
        x: Math.min(start.x, curX), y: Math.min(start.y, curY),
        width: Math.abs(curX - start.x), height: Math.abs(curY - start.y),
      });
    }
  }

  @HostListener('window:pointerup', ['$event'])
  onSvgPointerUp(_event: PointerEvent) {
    if (!this.dragState) return;
    if (this.dragState.kind === 'draw') {
      const r = this.pendingNewRect;
      if (r && r.width > 8 && r.height > 8) {
        this.typePickerOpen = true;
      } else {
        this.pendingNewRect = null;
      }
      this.drawingNewBox = false;
    }
    this.dragState = null;
  }

  // Whatever is CURRENTLY DISPLAYED for this row (before this edit)
  // decides whether the edit is a "correction" or a fresh "add" — NOT
  // whether an AI box merely still exists somewhere in the raw data.
  // An AI box that the auditor already explicitly deleted stays
  // 'auditor_added' if redrawn: the auditor rejected the AI's
  // determination, so a new box for the same type has no correction
  // lineage back to it, even though that old AI box is still sitting
  // untouched in authenticity_checks.boxes.
  private actionFor(key: string): EditAction {
    const row = this.partyRows.find(r => r.key === key);
    if (!row) return 'add';
    return (row.status === 'AI_DETECTED' || row.status === 'AUDITOR_CORRECTED') ? 'correct' : 'add';
  }

  private applyResize(rect: EditableRect, handle: 'nw' | 'ne' | 'sw' | 'se', dx: number, dy: number): EditableRect {
    let { x, y, width, height } = rect;
    if (handle === 'nw' || handle === 'sw') { x += dx; width -= dx; }
    if (handle === 'ne' || handle === 'se') { width += dx; }
    if (handle === 'nw' || handle === 'ne') { y += dy; height -= dy; }
    if (handle === 'sw' || handle === 'se') { height += dy; }
    return { x, y, width, height };
  }

  private clampRect(rect: EditableRect): EditableRect {
    const minSize = 4;
    let { x, y, width, height } = rect;
    width = Math.max(minSize, width);
    height = Math.max(minSize, height);
    x = Math.max(0, Math.min(x, this.sourceWidth - width));
    y = Math.max(0, Math.min(y, this.sourceHeight - height));
    width = Math.min(width, this.sourceWidth - x);
    height = Math.min(height, this.sourceHeight - y);
    return { x, y, width, height };
  }

  // Which evidence types the auditor can assign to a just-drawn box —
  // every type valid for this document_type, so an existing box can
  // also be deliberately replaced, not just a "Needs Location" one.
  get assignableTypesForNewBox(): PartyRowDef[] {
    return this.partyRowDefs;
  }

  assignNewBoxType(key: string) {
    if (!this.pendingNewRect) return;
    this.stagedEdits[key] = { evidenceType: key, action: this.actionFor(key), rect: { ...this.pendingNewRect } };
    this.pendingNewRect = null;
    this.typePickerOpen = false;
    this.selectedEditKey = key;
  }

  cancelDraw() {
    this.pendingNewRect = null;
    this.typePickerOpen = false;
    this.drawingNewBox = false;
  }

  handlePositions(box: VisionEvidenceBox): Record<'nw' | 'ne' | 'sw' | 'se', { x: number; y: number }> {
    const xs = box.polygon.map(p => p.x);
    const ys = box.polygon.map(p => p.y);
    const x1 = Math.min(...xs), x2 = Math.max(...xs), y1 = Math.min(...ys), y2 = Math.max(...ys);
    return { nw: { x: x1, y: y1 }, ne: { x: x2, y: y1 }, sw: { x: x1, y: y2 }, se: { x: x2, y: y2 } };
  }

  saveEdits() {
    if (!this.documentId || this.isSavingEdits || !this.hasStagedEdits) return;
    this.isSavingEdits = true;
    this.errorMessage = '';

    const changes = Object.values(this.stagedEdits).map(edit => {
      if (edit.action === 'delete' || edit.action === 'reset') {
        return { evidence_type: edit.evidenceType, action: edit.action };
      }
      const n = this.rectToNormalized(edit.rect!);
      return { evidence_type: edit.evidenceType, action: edit.action, page: 1, x: n.x, y: n.y, width: n.width, height: n.height };
    });

    this.http.put<any>(
      `${this.apiUrl}/authenticity/${this.documentId}/evidence-corrections?document_type=${this.documentType}`,
      { changes },
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.check.evidence_corrections = res.evidence_corrections;
        this.isSavingEdits = false;
        this.cancelEdits();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isSavingEdits = false;
        this.errorMessage = err.error?.error || 'Could not save evidence location changes.';
        this.cdr.detectChanges();
      }
    });
  }

  private rectToNormalized(rect: EditableRect): NormalizedRegion {
    return {
      page: 1,
      x: rect.x / this.sourceWidth, y: rect.y / this.sourceHeight,
      width: rect.width / this.sourceWidth, height: rect.height / this.sourceHeight,
    };
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

  // ── Supporting Evidence — up to 3 concise, deduplicated positives.
  // v10 dedup: no longer includes a combined "Supplier details are
  // consistent" item — Issuer/Supplier and Buyer are now shown, with
  // their own precise location, in the Parties panel above; repeating
  // them here would be exactly the duplication the spec asks to
  // remove. documentEvidenceRows is already deduped (see its getter),
  // so this loop can't reintroduce the stamp/supplier-info duplicates
  // either. ──

  get supportingEvidenceItems(): SupportingEvidenceItem[] {
    // Invoice: the Supporting Evidence card is entirely redundant with
    // the Invoice Parties panel above (Issuer/Buyer/Stamp already shown
    // there, each with its own precise location) — its only remaining
    // content would be the logo/signature rows, which already live in
    // View Full Analysis > Document Evidence (signature already reads
    // "Not Required" there, since it's never a required signal). PO/GR
    // are untouched — their Supporting Evidence card still summarizes
    // company_logo/supplier-info positives that have no other home.
    if (this.documentType === 'invoice') return [];
    const items: SupportingEvidenceItem[] = [];
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
    this.recheckMessage = null;
    this.cdr.detectChanges();

    this.http.post<any>(
      `${this.apiUrl}/authenticity/${this.documentId}/recheck?document_type=${this.documentType}`,
      {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.check = res;
        this.isRechecking = false;
        // A recheck regenerates every AI region from scratch (the whole
        // `boxes` column is replaced) but NEVER touches saved auditor
        // corrections (a separate table) — clear any stale on-image
        // emphasis/edit state from before the recheck, and tell the
        // auditor their corrections are still the ones being shown.
        this.emphasizedKey = null;
        this.cancelEdits();
        if ((res.evidence_corrections || []).length > 0) {
          this.recheckMessage = 'AI analysis updated. Auditor corrections retained.';
        }
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
