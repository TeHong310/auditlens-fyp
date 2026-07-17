import { Component, OnInit, OnDestroy, HostListener, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { environment } from '../../../environments/environment';

type DocType = 'invoice' | 'po' | 'gr';

@Component({
  selector: 'app-auditor-record-detail',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-record-detail.component.html',
  styleUrls: ['./auditor-record-detail.component.css']
})
export class AuditorRecordDetailComponent implements OnInit, OnDestroy {

  documentId: number | null = null;
  comparison: any = null;
  authenticity: any = null;
  isLoading: boolean = false;
  isSubmitting: boolean = false;
  successMessage: string = '';
  errorMessage: string = '';
  auditNote: string = '';

  // PDF quick-view modal
  showModal: boolean = false;
  modalDocType: DocType = 'invoice';
  modalFileName: string = '';
  modalIframeUrl: SafeResourceUrl | null = null;
  modalRawBlobUrl: string = '';
  modalLoading: boolean = false;
  modalError: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private route: ActivatedRoute,
    private cdr: ChangeDetectorRef,
    private sanitizer: DomSanitizer
  ) {}

  ngOnInit() {
    this.route.queryParams.subscribe(params => {
      if (params['document_id']) {
        this.documentId = parseInt(params['document_id']);
        this.loadComparison();
        this.loadAuthenticity();
      }
    });
  }

  ngOnDestroy() {
    this.revokeModalBlobUrl();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── Load comparison ─────────────────────────────────────

  loadComparison() {
    if (!this.documentId) return;
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/auditor/record/${this.documentId}/comparison`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.comparison = res;
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load record comparison.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Authenticity warning banner ─────────────────────────
  // Advisory only (Layer 6 soft gate) — informational, never blocks
  // the review flow below. A 404 (no check run / not yet detected)
  // is expected and silent, not an error.

  loadAuthenticity() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/authenticity/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.authenticity = res;
        this.cdr.detectChanges();
      },
      error: () => {
        this.authenticity = null;
      }
    });
  }

  get showAuthenticityWarning(): boolean {
    return this.authenticity?.authenticity_status === 'warning';
  }

  authenticityWarningReason(): string {
    if (!this.authenticity) return '';
    const missing: string[] = [];
    if (!this.authenticity.has_company_name) missing.push('company name');
    if (!this.authenticity.has_company_chop && !this.authenticity.has_signature) {
      missing.push('signature and company chop');
    }
    const docLabel = this.authenticity.document_type === 'invoice' ? 'Invoice'
      : this.authenticity.document_type === 'po' ? 'PO' : 'GR';
    if (missing.length === 0) return `Authenticity signals below expected threshold on ${docLabel}.`;
    return `Missing ${missing.join(' and ')} on ${docLabel}.`;
  }

  authenticitySourceIcon(): string {
    const source = this.authenticity?.upload_source;
    if (source === 'phone_photo') return '📱';
    if (source === 'scanned') return '🖨️';
    if (source === 'digital_native') return '💻';
    if (source === 'webcam') return '📷';
    return '❓';
  }

  authenticitySourceLabel(): string {
    const source = this.authenticity?.upload_source;
    if (source === 'phone_photo') return 'Phone Photo';
    if (source === 'scanned') return 'Scanned';
    if (source === 'digital_native') return 'Digital Native';
    if (source === 'webcam') return 'Webcam';
    return 'Unknown';
  }

  // ── Audit decision actions ──────────────────────────────

  approveDocument() {
    if (!this.documentId) return;
    this.isSubmitting = true;
    this.http.post<any>(`${this.apiUrl}/reviews/approve/${this.documentId}`,
      { remarks: this.auditNote },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = 'Document approved successfully!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/auditor/home']);
        }, 2000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.errorMessage = err.error?.error || 'Failed to approve.';
        this.cdr.detectChanges();
      }
    });
  }

  returnDocument() {
    if (!this.documentId) return;
    if (!this.auditNote) {
      this.errorMessage = 'Please add a note before returning the document.';
      this.cdr.detectChanges();
      return;
    }
    this.isSubmitting = true;
    this.http.post<any>(`${this.apiUrl}/reviews/return/${this.documentId}`,
      { remarks: this.auditNote },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = 'Document returned to Finance!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/auditor/home']);
        }, 2000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.errorMessage = err.error?.error || 'Failed to return.';
        this.cdr.detectChanges();
      }
    });
  }

  goBack() {
    this.router.navigate(['/auditor/home']);
  }

  // ── Overall status banner ───────────────────────────────

  get overallStatus(): string {
    return this.comparison?.match_result?.overall_status || 'PARTIAL';
  }

  getBannerClass(): string {
    if (this.overallStatus === 'PASS') return 'banner-pass';
    if (this.overallStatus === 'FAIL') return 'banner-fail';
    return 'banner-partial';
  }

  getBannerIcon(): string {
    if (this.overallStatus === 'PASS') return '✅';
    if (this.overallStatus === 'FAIL') return '❌';
    return '⚠️';
  }

  getBannerText(): string {
    if (this.overallStatus === 'PASS') return 'All Fields Match';
    if (this.overallStatus === 'FAIL') return 'Mismatch Detected';
    return 'Documents Incomplete';
  }

  getBannerSubtitle(): string {
    if (this.overallStatus === 'PASS') return 'Ready for approval';
    if (this.overallStatus === 'FAIL') return 'Review required — see highlighted rows';
    if (this.comparison && !this.comparison.po && !this.comparison.gr) return 'Awaiting PO and GR upload';
    if (this.comparison && !this.comparison.po) return 'Awaiting PO upload';
    if (this.comparison && !this.comparison.gr) return 'Awaiting GR upload';
    return 'Some documents missing';
  }

  // ── Field comparison table helpers ──────────────────────
  // Pairwise (Invoice->PO, PO->GR) symbols are computed client-side from
  // the raw values the API already returns, so the table can show a
  // per-column relationship instead of only the aggregate match flags.

  private normalizeVendor(name: string | null | undefined): string {
    if (!name) return '';
    return name.toLowerCase()
      .replace(/[.,()]/g, '')
      .replace(/\bsdn\s*bhd\b/g, '')
      .replace(/\bberhad\b/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  private amountsEqual(a: number | null | undefined, b: number | null | undefined): boolean {
    if (a === null || a === undefined || b === null || b === undefined) return false;
    return Math.abs(Number(a) - Number(b)) < 0.01;
  }

  vendorSymbol(fromVal: string | null, toVal: string | null): 'eq' | 'neq' | 'na' {
    if (!fromVal || !toVal) return 'na';
    return this.normalizeVendor(fromVal) === this.normalizeVendor(toVal) ? 'eq' : 'neq';
  }

  amountSymbol(fromVal: number | null, toVal: number | null): 'eq' | 'neq' | 'na' {
    if (fromVal === null || fromVal === undefined || toVal === null || toVal === undefined) return 'na';
    return this.amountsEqual(fromVal, toVal) ? 'eq' : 'neq';
  }

  rowClass(symbols: ('eq' | 'neq' | 'na')[]): string {
    return symbols.includes('neq') ? 'row-mismatch' : '';
  }

  matchPillClass(sym: 'eq' | 'neq' | 'na'): string {
    if (sym === 'eq') return 'pill-match';
    if (sym === 'neq') return 'pill-differ';
    return 'pill-na';
  }

  matchPillIcon(sym: 'eq' | 'neq' | 'na'): string {
    if (sym === 'eq') return '✓';
    if (sym === 'neq') return '✗';
    return '—';
  }

  matchPillText(sym: 'eq' | 'neq' | 'na'): string {
    if (sym === 'eq') return 'Match';
    if (sym === 'neq') return 'Differ';
    return 'N/A';
  }

  // ── 3-way row-level match indicator (PO Ref / Item / Quantity) ──
  // Unlike Vendor/Amount above (pairwise, computed client-side per
  // cell), these compare all three present values at once and are
  // computed server-side in match_result — one indicator per row,
  // shown once next to the field label, not per cell.

  rowMatchClass(match: boolean | null): string {
    return match === false ? 'row-mismatch' : '';
  }

  rowMatchPillClass(match: boolean | null): string {
    if (match === true) return 'pill-match';
    if (match === false) return 'pill-differ';
    return 'pill-na';
  }

  rowMatchIcon(match: boolean | null): string {
    if (match === true) return '✓';
    if (match === false) return '⚠';
    return '—';
  }

  rowMatchText(match: boolean | null): string {
    if (match === true) return 'Match';
    if (match === false) return 'Mismatch';
    return 'N/A';
  }

  formatQuantity(qty: any): string {
    if (qty === null || qty === undefined || qty === '') return '-';
    const n = parseFloat(qty);
    return Number.isInteger(n) ? String(n) : n.toFixed(2);
  }

  // ── Formatting ───────────────────────────────────────────

  formatAmount(amount: any): string {
    if (amount === null || amount === undefined || amount === '') return '-';
    return 'RM ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  // ── PDF quick-view modal ─────────────────────────────────

  private fileUrlFor(type: DocType): string | null {
    if (!this.comparison) return null;
    if (type === 'invoice') return `${this.apiUrl}/documents/${this.comparison.invoice.document_id}/file`;
    if (type === 'po' && this.comparison.po) return `${this.apiUrl}/documents/po/${this.comparison.po.po_id}/file`;
    if (type === 'gr' && this.comparison.gr) return `${this.apiUrl}/documents/gr/${this.comparison.gr.gr_id}/file`;
    return null;
  }

  fileNameFor(type: DocType): string {
    if (!this.comparison) return '';
    if (type === 'invoice') return this.comparison.invoice?.filename || '';
    if (type === 'po') return this.comparison.po?.filename || '';
    if (type === 'gr') return this.comparison.gr?.filename || '';
    return '';
  }

  docTypeLabel(type: DocType): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'Purchase Order';
    return 'Goods Receipt';
  }

  isDocAvailable(type: DocType): boolean {
    return !!this.fileUrlFor(type);
  }

  openDocModal(type: DocType) {
    const url = this.fileUrlFor(type);
    if (!url) return;

    this.revokeModalBlobUrl();
    this.modalDocType = type;
    this.modalFileName = this.fileNameFor(type);
    this.modalLoading = true;
    this.modalError = '';
    this.modalIframeUrl = null;
    this.showModal = true;
    this.cdr.detectChanges();

    this.http.get(url, { headers: this.getHeaders(), responseType: 'blob' }).subscribe({
      next: (blob) => {
        this.modalRawBlobUrl = URL.createObjectURL(blob);
        this.modalIframeUrl = this.sanitizer.bypassSecurityTrustResourceUrl(this.modalRawBlobUrl);
        this.modalLoading = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.modalLoading = false;
        this.modalError = 'Failed to load document.';
        this.cdr.detectChanges();
      }
    });
  }

  closeModal() {
    this.showModal = false;
    this.revokeModalBlobUrl();
    this.cdr.detectChanges();
  }

  openInNewTab() {
    if (this.modalRawBlobUrl) window.open(this.modalRawBlobUrl, '_blank');
  }

  downloadFile() {
    if (!this.modalRawBlobUrl) return;
    const a = document.createElement('a');
    a.href = this.modalRawBlobUrl;
    a.download = this.modalFileName || 'document';
    a.click();
  }

  private revokeModalBlobUrl() {
    if (this.modalRawBlobUrl) {
      URL.revokeObjectURL(this.modalRawBlobUrl);
      this.modalRawBlobUrl = '';
    }
  }

  @HostListener('document:keydown.escape')
  onEscapeKey() {
    if (this.showModal) this.closeModal();
  }
}
