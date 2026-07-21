import { Component, OnInit, OnDestroy, ViewChild, ElementRef, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type SignalKey = 'has_company_chop' | 'has_company_logo' | 'has_company_name' | 'has_signature';

interface OverlayMarker {
  key: SignalKey;
  shape: 'rect' | 'circle';
  present: boolean; // true = detected (green marker), false = missing but located (red marker)
  left: number;
  top: number;
  width: number;
  height: number;
}

// Wide text fields get a rounded-rectangle outline; compact marks get a
// circle. Signature isn't explicitly bucketed in the spec (only
// company_name -> rect and logo/chop -> circle are named) — grouped with
// company_name since a handwritten signature is a stroke/mark, not a
// compact round/square shape like a chop or logo.
const SHAPE_MAP: Record<SignalKey, 'rect' | 'circle'> = {
  has_company_name: 'rect',
  has_signature: 'rect',
  has_company_chop: 'circle',
  has_company_logo: 'circle',
};

const SIGNAL_LABELS: Record<SignalKey, string> = {
  has_company_name: 'Company Name',
  has_company_chop: 'Company Chop',
  has_signature: 'Signature',
  has_company_logo: 'Company Logo',
};

// New named-box overlay (check.boxes, populated once a document has been
// checked by the upgraded Claude/Gemini authentication engine) — 5-color
// legend per spec: Blue=Supplier Logo, Green=Company Name,
// Purple=Supplier Address, Red=Stamp/Chop, Orange=Signature. Keyed by the
// stable `type` machine key (not the display label) so relabeling never
// breaks color/click-highlight matching.
//
// coordinate_space/source_image_width/source_image_height/
// localization_quality are the v6 canonical-coordinate-contract fields
// (see helpers/authenticity_check.py::_normalize_box_to_unit_space) —
// all OPTIONAL on this interface because a legacy row saved before that
// fix won't have them; convertBBoxToDisplay() below falls back
// gracefully when they're absent.
export type CoordinateSpace = 'normalized_0_1' | 'normalized_0_1000' | 'native_pixels';
export type LocalizationQuality = 'exact' | 'approximate' | 'unreliable';

export interface NamedBox {
  type: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
  confidence: number;
  coordinate_space?: CoordinateSpace;
  source_image_width?: number;
  source_image_height?: number;
  localization_quality?: LocalizationQuality;
}

export interface DisplayBBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface NewOverlayMarker extends NamedBox {
  colorClass: string;
  quality: LocalizationQuality;
  left: number;
  top: number;
}

// v6 spec Step 5 — THE single bbox-to-display conversion function.
// Supports every coordinate_space a box might declare:
//   normalized_0_1     — x/y/width/height are fractions of the image
//                         (the CANONICAL format every NEW box is stored
//                         in — see _normalize_box_to_unit_space).
//   normalized_0_1000  — Gemini's own reliable convention (legacy
//                         signal_boxes-shaped data, if ever passed
//                         through this same function).
//   native_pixels      — raw pixel coordinates relative to either the
//                         box's own recorded source_image_width/height,
//                         or (for a LEGACY row saved before
//                         coordinate_space existed at all) the
//                         currently-loaded image's natural size — the
//                         last known-good assumption for that older
//                         data, since it's the same source file.
// Returns null if the image hasn't finished laying out yet (zero
// dimensions) — caller should skip rendering that marker for this pass;
// recomputeMarkers() re-runs on image load and on every resize, so a
// skipped marker is corrected on the very next call.
export function convertBBoxToDisplay(box: NamedBox, img: HTMLImageElement): DisplayBBox | null {
  const displayWidth = img.clientWidth;
  const displayHeight = img.clientHeight;
  const naturalWidth = img.naturalWidth;
  const naturalHeight = img.naturalHeight;
  if (!displayWidth || !displayHeight || !naturalWidth || !naturalHeight) return null;

  const space: CoordinateSpace = box.coordinate_space || 'native_pixels';
  let scaleX: number;
  let scaleY: number;

  if (space === 'normalized_0_1') {
    scaleX = displayWidth;
    scaleY = displayHeight;
  } else if (space === 'normalized_0_1000') {
    scaleX = displayWidth / 1000;
    scaleY = displayHeight / 1000;
  } else {
    const sourceWidth = box.source_image_width || naturalWidth;
    const sourceHeight = box.source_image_height || naturalHeight;
    scaleX = displayWidth / sourceWidth;
    scaleY = displayHeight / sourceHeight;
  }

  return {
    left: box.x * scaleX,
    top: box.y * scaleY,
    width: box.width * scaleX,
    height: box.height * scaleY,
  };
}

const BOX_COLOR_CLASS: Record<string, string> = {
  supplier_logo:    'box-blue',
  company_name:     'box-green',
  supplier_address: 'box-purple',
  company_stamp:    'box-red',
  signature:        'box-orange',
};

const EVIDENCE_KEYS = ['company_logo', 'company_name', 'supplier_address', 'stamp', 'signature'] as const;
type EvidenceKey = typeof EVIDENCE_KEYS[number];

const EVIDENCE_LABELS: Record<EvidenceKey, string> = {
  company_logo:     'Supplier Logo',
  company_name:     'Supplier Name',
  supplier_address: 'Supplier Address',
  stamp:            'Stamp / Chop',
  signature:        'Signature',
};

// v6 spec Step 4: document-type-aware wording. A PO/GR's own letterhead
// is normally the BUYER (see CLAUDE_AUTHENTICITY_PROMPT's document-type
// guidance), so there's usually no distinct SUPPLIER logo to find at
// all — the label makes that explicit instead of reading like a missed
// detection. GR's stamp is normally a receiving/QC stamp, not a chop.
const EVIDENCE_LABELS_BY_DOC_TYPE: Partial<Record<string, Partial<Record<EvidenceKey, string>>>> = {
  po: {
    company_logo: 'Supplier Logo (Optional)',
  },
  gr: {
    company_logo: 'Supplier Logo (Optional)',
    stamp:        'Receiving / QC Stamp',
  },
};

// Maps each evidence key to the box `type` it corresponds to (see
// helpers/authenticity_check.py's _BOX_TYPES) — used to match a sidebar
// row click to its overlay box.
const EVIDENCE_TO_BOX_TYPE: Record<EvidenceKey, string> = {
  company_logo:     'supplier_logo',
  company_name:     'company_name',
  supplier_address: 'supplier_address',
  stamp:            'company_stamp',
  signature:        'signature',
};

const CONSISTENCY_LABELS: Record<string, string> = {
  vendor_match: 'Vendor',
  po_match:     'PO Reference',
  item_match:   'Items',
  amount_match: 'Amount',
};

const RISK_LABELS: Record<string, string> = {
  copy_paste_risk:        'Copy/Paste Risk',
  font_consistency:       'Font Consistency',
  alignment_consistency:  'Alignment Consistency',
  alteration_risk:        'Alteration Risk',
};

@Component({
  selector: 'app-auditor-authenticity-detail',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-authenticity-detail.component.html',
  styleUrls: ['./auditor-authenticity-detail.component.css']
})
export class AuditorAuthenticityDetailComponent implements OnInit, OnDestroy {
  @ViewChild('docImage') docImageRef?: ElementRef<HTMLImageElement>;

  documentId: number | null = null;
  documentType: string = 'invoice';

  check: any = null;
  isLoading = false;
  errorMessage = '';
  isRechecking = false;

  imageBlobUrl: string | null = null;
  // idle -> loading -> one of: 'image' (loaded, overlay math runs),
  // 'error' (no file / fetch failed). The backend always serves an
  // image here — a PDF's rendered first page, or the original file if
  // it's already an image — so there's no separate PDF state to handle.
  imageLoadState: 'idle' | 'loading' | 'image' | 'error' = 'idle';
  markers: OverlayMarker[] = [];

  signalKeys: SignalKey[] = ['has_company_name', 'has_company_chop', 'has_signature', 'has_company_logo'];
  signalLabels = SIGNAL_LABELS;

  // New engine overlay/dashboard state — only populated once check.boxes /
  // check.ai_visual_result exist (a document checked by the upgraded
  // Claude/Gemini engine). Older, not-yet-rechecked rows fall back to the
  // markers/signalKeys checklist above.
  newMarkers: NewOverlayMarker[] = [];
  selectedBoxType: string | null = null;
  evidenceKeys = EVIDENCE_KEYS;
  evidenceLabels = EVIDENCE_LABELS;
  consistencyLabels = CONSISTENCY_LABELS;
  riskKeys = Object.keys(RISK_LABELS);
  riskLabels = RISK_LABELS;

  private resizeObserver: ResizeObserver | null = null;
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
    this.resizeObserver?.disconnect();
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
    // Always the authenticity image endpoint — it serves the rendered
    // PDF-page image (the same one Gemini vision saw) or the original
    // file if it's already an image, so overlay math always has a real
    // image to measure against, regardless of document_type/file kind.
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

  onImageLoad() {
    this.resizeObserver?.disconnect();
    if (this.docImageRef) {
      this.resizeObserver = new ResizeObserver(() => this.recomputeMarkers());
      this.resizeObserver.observe(this.docImageRef.nativeElement);
    }
    this.recomputeMarkers();
  }

  // Re-run on ResizeObserver so markers stay correct across window
  // resize / sidebar toggle / responsive reflow.
  //
  // TWO DIFFERENT coordinate systems are in play here, confirmed against
  // real production data (see git history for this fix):
  //   - check.signal_boxes (legacy, Gemini-only AUTHENTICITY_PROMPT path)
  //     is genuinely 0-1000-normalized — a well-documented, reliable
  //     Gemini vision API convention — so it's scaled by
  //     renderedSize/1000, unchanged from before.
  //   - check.boxes (the newer Claude-generated boxes, see helpers/
  //     authenticity_check.py::_flatten_boxes) are NATIVE PIXEL
  //     coordinates matching the rendered PNG's actual dimensions —
  //     Claude does not reliably follow the prompt's "normalize to
  //     0-1000" instruction the way Gemini's vision API does. These are
  //     scaled from the image's own naturalWidth/naturalHeight (its
  //     intrinsic pixel size, available once the <img> 'load' event has
  //     fired) to its DISPLAYED size — scaleX = displayWidth/naturalWidth
  //     — never by a hardcoded /1000.
  recomputeMarkers() {
    if (!this.docImageRef) {
      this.markers = [];
      this.newMarkers = [];
      this.cdr.detectChanges();
      return;
    }
    const img = this.docImageRef.nativeElement;
    const displayWidth = img.clientWidth;
    const displayHeight = img.clientHeight;
    const naturalWidth = img.naturalWidth;
    const naturalHeight = img.naturalHeight;
    if (!displayWidth || !displayHeight || !naturalWidth || !naturalHeight) return;

    // Legacy overlay only — see class comment above.
    const legacyScaleX = displayWidth / 1000;
    const legacyScaleY = displayHeight / 1000;

    // A signal has a marker whenever the backend kept a box for it,
    // regardless of present/missing — present=green, missing=red (a
    // missing signal can still have a plausible location, e.g. a blank
    // signature line). check[key] is the presence boolean.
    const markers: OverlayMarker[] = [];
    if (this.check?.signal_boxes) {
      for (const key of this.signalKeys) {
        const box = this.check.signal_boxes[key];
        if (!Array.isArray(box) || box.length !== 4) continue;
        const [ymin, xmin, ymax, xmax] = box;
        markers.push({
          key,
          shape: SHAPE_MAP[key],
          present: !!this.check[key],
          left: xmin * legacyScaleX,
          top: ymin * legacyScaleY,
          width: (xmax - xmin) * legacyScaleX,
          height: (ymax - ymin) * legacyScaleY,
        });
      }
    }
    this.markers = markers;

    // check.boxes — canonical coordinate-contract conversion (v6 fix).
    // convertBBoxToDisplay() reads each box's OWN declared
    // coordinate_space rather than a single hardcoded assumption for
    // the whole array, so this is correct regardless of whether a given
    // row was saved under the old (ambiguous) or new (canonical) shape.
    const newMarkers: NewOverlayMarker[] = [];
    if (Array.isArray(this.check?.boxes)) {
      for (const box of this.check.boxes as NamedBox[]) {
        const renderedBBox = convertBBoxToDisplay(box, img);
        if (!renderedBBox) continue;

        // TEMPORARY, development-only — remove/disable once bbox
        // alignment is confirmed correct against real documents.
        console.log('AUTH BBOX TRACE', {
          documentType: this.documentType,
          evidenceType: box.type,
          coordinateSpace: box.coordinate_space || 'native_pixels (legacy row, no coordinate_space stored)',
          sourceImageWidth: box.source_image_width,
          sourceImageHeight: box.source_image_height,
          naturalWidth, naturalHeight,
          clientWidth: displayWidth, clientHeight: displayHeight,
          rawBBox: { x: box.x, y: box.y, width: box.width, height: box.height },
          normalizedBBox: box.coordinate_space === 'normalized_0_1'
            ? { x: box.x, y: box.y, width: box.width, height: box.height }
            : null,
          renderedBBox,
        });

        const quality: LocalizationQuality = box.localization_quality || 'exact';
        if (quality === 'unreliable') {
          // v6 spec Step 3: never draw a misleading rectangle for a box
          // the AI itself wasn't confident about the exact location of
          // — the sidebar still shows it as detected (see
          // localizationNote()), just without a box on the image.
          continue;
        }

        newMarkers.push({
          ...box,
          colorClass: BOX_COLOR_CLASS[box.type] || 'box-blue',
          quality,
          ...renderedBBox,
        });
      }
    }
    this.newMarkers = newMarkers;

    this.cdr.detectChanges();
  }

  // ── New engine dashboard/overlay helpers ──

  get hasNewResult(): boolean {
    return !!this.check?.ai_visual_result;
  }

  get supplierIdentity(): any {
    return this.check?.ai_visual_result?.supplier_identity || null;
  }

  get visualEvidence(): any {
    return this.check?.ai_visual_result?.document_visual_evidence || null;
  }

  get integrityCheck(): any {
    return this.check?.ai_visual_result?.integrity_check || null;
  }

  get overallResult(): any {
    return this.check?.ai_visual_result?.overall_result || null;
  }

  get documentConsistency(): any {
    return this.check?.document_consistency || null;
  }

  // v4: deterministic auditor score/decision — see helpers/
  // authenticity_check.py::_compute_auditor_score(). Computed at check
  // time, stored inside ai_visual_result.
  get auditorScore(): any {
    return this.check?.ai_visual_result?.auditor_score || null;
  }

  // v4: cross-document comparison (Invoice/PO/GR supplier identity +
  // reference numbers + items + date order) — computed fresh on every
  // load, not stored; null until at least 2 of the 3 document types
  // have been checked.
  get crossDocumentAuthenticity(): any {
    return this.check?.cross_document_authenticity || null;
  }

  decisionClass(decision: string): string {
    if (decision === 'APPROVE') return 'decision-approve';
    if (decision === 'REVIEW') return 'decision-review';
    return 'decision-reject';
  }

  scoreClass(score: number): string {
    if (score >= 85) return 'risk-low';
    if (score >= 60) return 'risk-medium';
    return 'risk-high';
  }

  get consistencyKeys(): string[] {
    return Object.keys(CONSISTENCY_LABELS);
  }

  evidenceEntry(key: EvidenceKey): any {
    return this.visualEvidence?.[key] || null;
  }

  riskLevelClass(level: string): string {
    const l = (level || '').toUpperCase();
    if (l === 'HIGH') return 'risk-high';
    if (l === 'MEDIUM') return 'risk-medium';
    return 'risk-low';
  }

  matchLabel(value: boolean | null | undefined): string {
    if (value === true) return 'Matched';
    if (value === false) return 'Mismatch';
    return 'N/A';
  }

  matchClass(value: boolean | null | undefined): string {
    if (value === true) return 'icon-yes';
    if (value === false) return 'icon-no';
    return 'icon-na';
  }

  // A signal that isn't required for this document type (e.g. a
  // signature on a computer-generated invoice) must not render as a
  // false-negative red X — shows a neutral "Not required" state instead.
  evidenceStatusClass(key: EvidenceKey): string {
    const entry = this.evidenceEntry(key);
    if (!entry) return 'icon-na';
    if (entry.status === 'detected' || entry.detected) return 'icon-yes';
    if (entry.required === false) return 'icon-na';
    return 'icon-no';
  }

  // v4: stamp classification (company_chop/received_stamp/qc_stamp/
  // approval_stamp) — shown alongside the Stamp/Chop evidence row.
  stampTypeLabel(): string {
    const type = this.evidenceEntry('stamp')?.type;
    if (!type) return '';
    return type.split('_').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  }

  evidenceStatusLabel(key: EvidenceKey): string {
    const entry = this.evidenceEntry(key);
    if (!entry) return 'Unknown';
    if (entry.status === 'detected' || entry.detected) return 'Detected';
    if (entry.required === false) return 'Not required';
    return 'Not Detected';
  }

  // v6 spec Step 4: document-type-aware evidence wording — e.g. "Supplier
  // Logo (Optional)" on a PO/GR, since their own letterhead is normally
  // the buyer, not the supplier, so there's usually no distinct supplier
  // logo to find at all.
  evidenceLabelFor(key: EvidenceKey): string {
    return EVIDENCE_LABELS_BY_DOC_TYPE[this.documentType]?.[key] || EVIDENCE_LABELS[key];
  }

  // v6 spec Step 3: for a box whose localization_quality is "unreliable"
  // (excluded from the image overlay in recomputeMarkers()), surfaces
  // that it WAS detected without implying a precise location is known.
  // "approximate" boxes ARE drawn (dashed, see the template/CSS) so no
  // separate note is needed for that case.
  localizationNote(key: EvidenceKey): string {
    const box = this.evidenceEntry(key)?.boxes;
    if (!box) return '';
    if (box.localization_quality === 'unreliable') return 'Detected, exact location unavailable';
    return '';
  }

  supplierStatusClass(): string {
    const status = this.supplierIdentity?.status;
    if (status === 'verified') return 'badge-verified';
    if (status === 'uncertain') return 'badge-uncertain';
    return 'badge-not-found';
  }

  supplierStatusLabel(): string {
    const status = this.supplierIdentity?.status;
    if (status === 'verified') return 'Verified';
    if (status === 'uncertain') return 'Uncertain';
    return 'Not Found';
  }

  // Cross-check against the vendor_name the (separate) extraction
  // pipeline already found — the top-priority supplier-identity signal
  // (v3 spec objective 4). null = nothing to compare (no extracted
  // vendor_name, or the vision engine found no supplier_name at all).
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

  get vendorMatchClass(): string {
    const supplier = this.supplierIdentity;
    if (!supplier || supplier.vendor_name_matches_extraction === null || supplier.vendor_name_matches_extraction === undefined) {
      return '';
    }
    return supplier.vendor_name_matches_extraction ? 'match-note-ok' : 'match-note-warn';
  }

  // Click a sidebar evidence row (section 8: interactive highlight) —
  // pulses/highlights the matching box on the image, if one was located.
  selectBox(type: string) {
    this.selectedBoxType = this.selectedBoxType === type ? null : type;
  }

  boxLabelFor(key: EvidenceKey): string {
    return EVIDENCE_LABELS[key];
  }

  boxTypeFor(key: EvidenceKey): string {
    return EVIDENCE_TO_BOX_TYPE[key];
  }

  // ── Re-check: the only action on this page that calls Gemini ──

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
        // Same file, same <img> src -> the image won't re-fire 'load',
        // so recompute markers directly against the fresh signal_boxes.
        this.recomputeMarkers();
      },
      error: (err) => {
        this.isRechecking = false;
        this.errorMessage = err.error?.error || 'Re-check failed.';
        this.cdr.detectChanges();
      }
    });
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

  hasBox(key: SignalKey): boolean {
    return !!this.check?.signal_boxes?.[key];
  }

  get hasAnyBoxes(): boolean {
    if (Array.isArray(this.check?.boxes) && this.check.boxes.length > 0) return true;
    return !!this.check?.signal_boxes && Object.keys(this.check.signal_boxes).length > 0;
  }
}
