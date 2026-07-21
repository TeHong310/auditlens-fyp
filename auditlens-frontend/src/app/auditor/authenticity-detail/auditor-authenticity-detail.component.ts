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
// Purple=Supplier Address, Red=Stamp/Chop, Orange=Signature.
interface NamedBox {
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

interface NewOverlayMarker extends NamedBox {
  colorClass: string;
  left: number;
  top: number;
}

const BOX_COLOR_CLASS: Record<string, string> = {
  'Supplier Logo':    'box-blue',
  'Company Name':     'box-green',
  'Supplier Address': 'box-purple',
  'Stamp/Chop':       'box-red',
  'Signature':        'box-orange',
};

const EVIDENCE_KEYS = ['company_logo', 'company_name', 'supplier_address', 'stamp', 'signature'] as const;
type EvidenceKey = typeof EVIDENCE_KEYS[number];

const EVIDENCE_LABELS: Record<EvidenceKey, string> = {
  company_logo:     'Company Logo',
  company_name:     'Company Name',
  supplier_address: 'Supplier Address',
  stamp:            'Stamp / Chop',
  signature:        'Signature',
};

const CONSISTENCY_LABELS: Record<string, string> = {
  vendor_match: 'Vendor',
  po_match:     'PO Reference',
  item_match:   'Items',
  amount_match: 'Amount',
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
  selectedBoxName: string | null = null;
  evidenceKeys = EVIDENCE_KEYS;
  evidenceLabels = EVIDENCE_LABELS;
  consistencyLabels = CONSISTENCY_LABELS;

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

  // Scales each [ymin,xmin,ymax,xmax] (normalized 0-1000) to the image's
  // actual rendered pixel size. Re-run on ResizeObserver so markers stay
  // correct across window resize / sidebar toggle / responsive reflow.
  //
  // Also builds newMarkers from check.boxes (already x/y/width/height,
  // same 0-1000 normalization — see helpers/authenticity_check.py's
  // _flatten_boxes) using the identical scaleX/scaleY math, just without
  // the corner-to-width/height conversion the legacy path needs.
  recomputeMarkers() {
    if (!this.docImageRef) {
      this.markers = [];
      this.newMarkers = [];
      this.cdr.detectChanges();
      return;
    }
    const img = this.docImageRef.nativeElement;
    const renderedWidth = img.clientWidth;
    const renderedHeight = img.clientHeight;
    if (!renderedWidth || !renderedHeight) return;

    const scaleX = renderedWidth / 1000;
    const scaleY = renderedHeight / 1000;

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
          left: xmin * scaleX,
          top: ymin * scaleY,
          width: (xmax - xmin) * scaleX,
          height: (ymax - ymin) * scaleY,
        });
      }
    }
    this.markers = markers;

    const newMarkers: NewOverlayMarker[] = [];
    if (Array.isArray(this.check?.boxes)) {
      for (const box of this.check.boxes as NamedBox[]) {
        newMarkers.push({
          ...box,
          colorClass: BOX_COLOR_CLASS[box.name] || 'box-blue',
          left: box.x * scaleX,
          top: box.y * scaleY,
          width: box.width * scaleX,
          height: box.height * scaleY,
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

  get consistencyKeys(): string[] {
    return Object.keys(CONSISTENCY_LABELS);
  }

  evidenceEntry(key: EvidenceKey): any {
    return this.visualEvidence?.[key] || null;
  }

  riskLevelClass(level: string): string {
    if (level === 'HIGH') return 'risk-high';
    if (level === 'MEDIUM') return 'risk-medium';
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

  // Click a sidebar evidence row (section 8: interactive highlight) —
  // pulses/highlights the matching box on the image, if one was located.
  selectBox(name: string) {
    this.selectedBoxName = this.selectedBoxName === name ? null : name;
  }

  boxLabelFor(key: EvidenceKey): string {
    return EVIDENCE_LABELS[key];
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
