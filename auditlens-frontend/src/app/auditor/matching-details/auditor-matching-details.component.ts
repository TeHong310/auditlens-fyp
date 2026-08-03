import { Component, OnInit, OnDestroy, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { environment } from '../../../environments/environment';

type DocType = 'invoice' | 'po' | 'gr';

// Three-Way Matching Details — split out of Record Detail (now the
// Audit Review page) so the Invoice/PO/GR field-by-field comparison,
// Source Documents, and Enterprise Matching Summary live on their own
// page instead of crowding the review workflow. Every value here comes
// from the SAME GET /auditor/record/<id>/comparison and GET /auditor/
// transactions/<id> endpoints Record Detail already used for this
// content — no new backend route, no new calculation, this component
// is purely the moved presentation layer for that data.
@Component({
  selector: 'app-auditor-matching-details',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-matching-details.component.html',
  styleUrls: ['./auditor-matching-details.component.css']
})
export class AuditorMatchingDetailsComponent implements OnInit, OnDestroy {

  documentId: number | null = null;
  comparison: any = null;
  transactionDetail: any = null;
  isLoading: boolean = false;
  errorMessage: string = '';

  // Original Document Viewer (left panel) — tab state + blob-preview
  // fetch, same fetch/sanitize logic the old PDF quick-view modal used.
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

  // Returns to the SAME record's Audit Review page — never the global
  // Review Queue/Anomaly Detection/Exceptions list — preserving both
  // document_id and (when this invoice belongs to one) transaction_
  // package_id exactly as Record Detail itself expects them.
  backToAuditReview() {
    const queryParams: any = { document_id: this.documentId };
    const pkgId = this.comparison?.transaction_context?.transaction_package_id;
    if (pkgId) queryParams.transaction_package_id = pkgId;
    this.router.navigate(['/auditor/record-detail'], { queryParams });
  }

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
        this.openDocModal('invoice');
        this.cdr.detectChanges();
        if (res.transaction_context) {
          this.loadTransactionDetail(res.transaction_context.transaction_package_id);
        }
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load record comparison.';
        this.cdr.detectChanges();
      }
    });
  }

  loadTransactionDetail(packageId: number) {
    this.http.get<any>(`${this.apiUrl}/auditor/transactions/${packageId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.transactionDetail = res;
        this.cdr.detectChanges();
      },
      error: () => { /* non-blocking — Enterprise Matching Summary simply stays hidden */ }
    });
  }

  // ── Field comparison table helpers (moved verbatim from Record
  // Detail — same logic, same wording, only the home file changed) ──

  private normalizeVendor(name: string | null | undefined): string {
    if (!name) return '';
    return name.toLowerCase()
      .replace(/[.,()]/g, ' ')
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

  amountSymbol(fromVal: number | null, toVal: number | null, fromCurrency?: string | null, toCurrency?: string | null): 'eq' | 'neq' | 'na' {
    if (fromVal === null || fromVal === undefined || toVal === null || toVal === undefined) return 'na';
    if (fromCurrency && toCurrency && fromCurrency.toUpperCase() !== toCurrency.toUpperCase()) return 'na';
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
    if (sym === 'eq') return 'ph-check';
    if (sym === 'neq') return 'ph-x';
    return 'ph-minus';
  }

  matchPillText(sym: 'eq' | 'neq' | 'na'): string {
    if (sym === 'eq') return 'Match';
    if (sym === 'neq') return 'Differ';
    return 'N/A';
  }

  rowMatchClass(match: boolean | null): string {
    return match === false ? 'row-mismatch' : '';
  }

  rowMatchPillClass(match: boolean | null): string {
    if (match === true) return 'pill-match';
    if (match === false) return 'pill-differ';
    return 'pill-na';
  }

  rowMatchIcon(match: boolean | null): string {
    if (match === true) return 'ph-check';
    if (match === false) return 'ph-warning';
    return 'ph-minus';
  }

  rowMatchText(match: boolean | null): string {
    if (match === true) return 'Match';
    if (match === false) return 'Mismatch';
    return 'N/A';
  }

  private lineItemMissing(li: any, side: 'po' | 'gr'): boolean {
    return side === 'po' ? !!li.missing_on_po : !!li.missing_on_gr;
  }

  get isV2Allocated(): boolean {
    return this.comparison?.engine_version === 'v2' && !!this.comparison?.invoice_result;
  }

  get amountCompareBasis(): number | null {
    if (this.isV2Allocated && this.comparison.invoice_result.allocated_amount != null) {
      return this.comparison.invoice_result.allocated_amount;
    }
    return this.comparison?.po?.total_amount ?? null;
  }

  isPartialAllocationLineItem(li: any): boolean {
    if (!this.isV2Allocated || this.comparison.invoice_result.status !== 'PASS') return false;
    if (li.po_quantity == null || li.invoice_quantity == null) return false;
    return li.invoice_quantity <= li.po_quantity + 0.01;
  }

  lineItemPoDisplayQuantity(li: any): any {
    if (li.po_quantity !== li.invoice_quantity && this.isPartialAllocationLineItem(li)) {
      return li.invoice_quantity;
    }
    return li.po_quantity;
  }

  showPoAllocationContext(li: any): boolean {
    return li.po_quantity !== li.invoice_quantity && this.isPartialAllocationLineItem(li);
  }

  private descriptionDiffers(a: string | null | undefined, b: string | null | undefined): boolean {
    if (!a || !b) return false;
    return a.trim().toLowerCase() !== b.trim().toLowerCase();
  }

  showPoDescription(li: any): boolean {
    return !li.missing_on_po && this.descriptionDiffers(li.po_description, li.description);
  }

  showGrDescription(li: any): boolean {
    return !li.missing_on_gr && this.descriptionDiffers(li.gr_description, li.description);
  }

  private lineItemSideQuantityMatch(li: any, side: 'po' | 'gr'): boolean | null {
    const sideQty = side === 'po' ? li.po_quantity : li.gr_quantity;
    if (sideQty == null || li.invoice_quantity == null) return null;
    return Math.abs(sideQty - li.invoice_quantity) < 0.01;
  }

  private lineItemSideHasIssue(li: any, side: 'po' | 'gr'): boolean {
    if (this.lineItemMissing(li, side)) return true;
    if (side === 'po' && this.isPartialAllocationLineItem(li)) return false;
    return this.lineItemSideQuantityMatch(li, side) === false;
  }

  lineItemRowClass(li: any): string[] {
    const hardIssue = this.lineItemSideHasIssue(li, 'po') || this.lineItemSideHasIssue(li, 'gr')
      || li.missing_on_invoice;
    if (hardIssue) return ['row-mismatch', 'row-quantity-alert'];
    if (li.amount_match === false && !this.isPartialAllocationLineItem(li)) return ['row-mismatch'];
    return [];
  }

  lineItemPillClass(li: any, side: 'po' | 'gr'): string {
    if (this.lineItemMissing(li, side)) return 'pill-differ';
    return this.lineItemSideHasIssue(li, side) ? 'pill-differ' : 'pill-match';
  }

  lineItemPillIcon(li: any, side: 'po' | 'gr'): string {
    if (this.lineItemMissing(li, side)) return 'ph-warning';
    return this.lineItemSideHasIssue(li, side) ? 'ph-x' : 'ph-check';
  }

  lineItemPillText(li: any, side: 'po' | 'gr'): string {
    if (side === 'po' && li.missing_on_po) return 'Missing on PO';
    if (side === 'gr' && li.missing_on_gr) return 'Missing on GR';
    return this.lineItemSideHasIssue(li, side) ? 'Mismatch' : 'Match';
  }

  formatQuantity(qty: any): string {
    if (qty === null || qty === undefined || qty === '') return '-';
    const n = parseFloat(qty);
    return Number.isInteger(n) ? String(n) : n.toFixed(2);
  }

  get fulfilmentSummaryText(): string {
    const pf = this.comparison?.po_fulfilment || [];
    if (!pf.length) return 'N/A';
    return pf.every((p: any) => p.status === 'FULLY_FULFILLED') ? 'Fully Matched' : 'Review Required';
  }

  get fulfilmentSummaryClass(): string {
    return this.fulfilmentSummaryText === 'Fully Matched' ? 'pill-match' : 'pill-differ';
  }

  formatAmount(amount: any, currency?: string | null): string {
    if (amount === null || amount === undefined || amount === '') return '-';
    return (currency || 'RM') + ' ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  // ── Related Documents / Source Documents click-through (moved
  // verbatim from Record Detail) ──

  openTransactionDocument(type: 'invoice' | 'po' | 'gr', documentId: number) {
    if (type === 'invoice') {
      if (documentId === this.documentId) return;
      // A sibling invoice in the same package opens ITS OWN Matching
      // Details page (not Audit Review) — staying on the same kind of
      // page the auditor is already viewing, exactly like Record
      // Detail's own equivalent used to navigate to another invoice's
      // Record Detail.
      this.router.navigate(['/auditor/matching-details'], { queryParams: { document_id: documentId } });
      return;
    }
    const path = type === 'po' ? `po/${documentId}/file` : `gr/${documentId}/file`;
    const token = localStorage.getItem('access_token');
    fetch(`${this.apiUrl}/documents/${path}`, { headers: { 'Authorization': `Bearer ${token}` } })
      .then(res => { if (!res.ok) throw new Error('Failed'); return res.blob(); })
      .then(blob => window.open(URL.createObjectURL(blob), '_blank'))
      .catch(() => { this.errorMessage = 'Failed to open file.'; this.cdr.detectChanges(); });
  }

  openSourceInvoice(documentId: number) {
    if (documentId === this.documentId) {
      this.openDocModal('invoice');
    } else {
      this.openTransactionDocument('invoice', documentId);
    }
  }

  // ── PDF quick-view modal (moved verbatim from Record Detail) ──

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

  isDocAvailable(type: DocType): boolean {
    return !!this.fileUrlFor(type);
  }

  openDocModal(type: DocType) {
    this.revokeModalBlobUrl();
    this.modalDocType = type;
    this.modalFileName = this.fileNameFor(type);
    this.modalError = '';
    this.modalIframeUrl = null;

    const url = this.fileUrlFor(type);
    if (!url) {
      this.modalLoading = false;
      this.cdr.detectChanges();
      return;
    }

    this.modalLoading = true;
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

}
