import { Component, OnInit, OnDestroy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
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

const CONSISTENCY_LABELS: Record<string, string> = {
  vendor_match: 'Vendor Match',
  po_match:     'PO Reference Match',
  item_match:   'Items Match',
  amount_match: 'Amount Match',
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

  consistencyLabels = CONSISTENCY_LABELS;
  riskKeys = Object.keys(RISK_LABELS);
  riskLabels = RISK_LABELS;

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

  // ── New engine dashboard helpers ──

  get hasNewResult(): boolean {
    return !!this.check?.ai_visual_result;
  }

  get supplierIdentity(): any {
    return this.check?.ai_visual_result?.supplier_identity || null;
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

  get auditorScore(): any {
    return this.check?.ai_visual_result?.auditor_score || null;
  }

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

  // Shared row status -> icon mapping used by every evidence-style
  // checklist row on this page. Red ("icon-no") is reserved for genuine
  // contradictions (e.g. a mismatched vendor name) — a signal that
  // simply wasn't found, but was required, reads as amber "Needs Review"
  // instead of a false-alarm red X.
  rowIconClass(status: RowStatus): string {
    return { yes: 'icon-yes', no: 'icon-no', warn: 'icon-warn', na: 'icon-na' }[status];
  }

  rowIcon(status: RowStatus): string {
    return { yes: 'ph-check', no: 'ph-x', warn: 'ph-warning', na: 'ph-minus' }[status];
  }

  // ── Section A: Supplier Identity ──

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

  supplierNameStatus(): RowStatus {
    return this.supplierIdentity?.supplier_name_detected ? 'yes' : 'warn';
  }

  supplierAddressStatus(): RowStatus {
    return this.supplierIdentity?.address_detected ? 'yes' : 'warn';
  }

  // Cross-check against the vendor_name the (separate) extraction
  // pipeline already found — a genuine mismatch here is a real
  // contradiction, so it's the one row in this section allowed to show
  // red.
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

  // ── Section B: Document Evidence (document-type-specific wording) ──
  // Enterprise V3 Phase 7 (FIX 3): delegates to the shared util also used
  // by the Authenticity list page's "Detected Signals" badges, so both
  // pages can never disagree about the same document again.

  get documentEvidenceRows(): EvidenceRow[] {
    return getAuthenticityEvidenceRows(this.check, this.documentType);
  }

  // ── Final recommendation ──

  get finalAssessmentNote(): string {
    const risk = (this.integrityCheck?.alteration_risk || '').toLowerCase();
    if (risk === 'high') return 'The system flagged possible visual alteration — review closely.';
    if (risk === 'medium') return 'The system found some inconsistencies worth a closer look.';
    return 'The system found no obvious visual alteration.';
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
}
