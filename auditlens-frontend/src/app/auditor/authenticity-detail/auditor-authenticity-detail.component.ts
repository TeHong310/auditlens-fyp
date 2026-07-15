import { Component, OnInit, OnDestroy, ViewChild, ElementRef, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type SignalKey = 'has_company_chop' | 'has_company_logo' | 'has_company_name' | 'has_signature';

interface OverlayMarker {
  key: SignalKey;
  shape: 'rect' | 'circle';
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
  // idle -> loading -> one of: 'image' (renderable, overlay math runs),
  // 'pdf' (file exists but isn't an image — overlays don't apply to a
  // raw PDF), 'error' (no file / fetch failed)
  imageLoadState: 'idle' | 'loading' | 'image' | 'pdf' | 'error' = 'idle';
  markers: OverlayMarker[] = [];

  signalKeys: SignalKey[] = ['has_company_name', 'has_company_chop', 'has_signature', 'has_company_logo'];
  signalLabels = SIGNAL_LABELS;

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
    if (this.documentType === 'invoice') return `${this.apiUrl}/documents/${this.documentId}/file`;
    if (this.documentType === 'po' && this.check.po_id) return `${this.apiUrl}/documents/po/${this.check.po_id}/file`;
    if (this.documentType === 'gr' && this.check.gr_id) return `${this.apiUrl}/documents/gr/${this.check.gr_id}/file`;
    return null;
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
        // Bounding boxes only make sense drawn over an <img>. A raw PDF
        // blob can't be rendered as one, so overlays are skipped for
        // PDFs — checklist still shows, just without image markers.
        this.imageLoadState = blob.type === 'application/pdf' ? 'pdf' : 'image';
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
  recomputeMarkers() {
    if (!this.docImageRef || !this.check?.signal_boxes) {
      this.markers = [];
      this.cdr.detectChanges();
      return;
    }
    const img = this.docImageRef.nativeElement;
    const renderedWidth = img.clientWidth;
    const renderedHeight = img.clientHeight;
    if (!renderedWidth || !renderedHeight) return;

    const scaleX = renderedWidth / 1000;
    const scaleY = renderedHeight / 1000;

    const markers: OverlayMarker[] = [];
    for (const key of this.signalKeys) {
      const box = this.check.signal_boxes[key];
      if (!Array.isArray(box) || box.length !== 4) continue;
      const [ymin, xmin, ymax, xmax] = box;
      markers.push({
        key,
        shape: SHAPE_MAP[key],
        left: xmin * scaleX,
        top: ymin * scaleY,
        width: (xmax - xmin) * scaleX,
        height: (ymax - ymin) * scaleY,
      });
    }
    this.markers = markers;
    this.cdr.detectChanges();
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
    if (source === 'phone_photo') return '📱';
    if (source === 'scanned') return '🖨️';
    if (source === 'digital_native') return '💻';
    if (source === 'webcam') return '📷';
    return '❓';
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
    return !!this.check?.signal_boxes && Object.keys(this.check.signal_boxes).length > 0;
  }
}
