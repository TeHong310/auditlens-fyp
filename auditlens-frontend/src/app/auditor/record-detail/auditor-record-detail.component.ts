import { Component, OnInit, OnDestroy, HostListener, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { environment } from '../../../environments/environment';

type DocType = 'invoice' | 'po' | 'gr';

// ── Send-Back structured form (Feature 1) — machine keys mirror
// helpers/send_back.py's REASON_CATEGORIES / REQUIRED_ACTIONS / PRIORITIES
// exactly, so the payload sent to POST /reviews/return/<id> validates
// cleanly server-side. Labels are the only thing translated to English
// here; the backend never sees the label text. ──
export type ReasonCategory =
  | 'missing_document' | 'incorrect_extracted_information' | 'invoice_po_gr_mismatch'
  | 'possible_duplicate_invoice' | 'authenticity_evidence_requires_clarification'
  | 'incorrect_supplier_information' | 'amount_or_quantity_requires_verification' | 'other';

export type RequiredAction =
  | 'upload_missing_document' | 'correct_extracted_information' | 'provide_written_explanation'
  | 'confirm_duplicate_submission' | 'replace_incorrect_document' | 'verify_amount_or_quantity'
  | 'confirm_supplier_information' | 'other';

export type Priority = 'normal' | 'medium' | 'high';

export interface SendBackFormState {
  reasonCategory: ReasonCategory | '';
  reasonOtherNote: string;
  instruction: string;
  requiredActions: RequiredAction[];
  requiredActionOtherNote: string;
  priority: Priority;
  dueDate: string;
}

const REASON_CATEGORY_OPTIONS: { key: ReasonCategory; label: string }[] = [
  { key: 'missing_document', label: 'Missing document' },
  { key: 'incorrect_extracted_information', label: 'Incorrect extracted information' },
  { key: 'invoice_po_gr_mismatch', label: 'Invoice / PO / GR mismatch' },
  { key: 'possible_duplicate_invoice', label: 'Possible duplicate invoice' },
  { key: 'authenticity_evidence_requires_clarification', label: 'Authenticity evidence requires clarification' },
  { key: 'incorrect_supplier_information', label: 'Incorrect supplier information' },
  { key: 'amount_or_quantity_requires_verification', label: 'Amount or quantity requires verification' },
  { key: 'other', label: 'Other' },
];

const REQUIRED_ACTION_OPTIONS: { key: RequiredAction; label: string }[] = [
  { key: 'upload_missing_document', label: 'Upload missing document' },
  { key: 'correct_extracted_information', label: 'Correct extracted information' },
  { key: 'provide_written_explanation', label: 'Provide written explanation' },
  { key: 'confirm_duplicate_submission', label: 'Confirm duplicate submission' },
  { key: 'replace_incorrect_document', label: 'Replace incorrect document' },
  { key: 'verify_amount_or_quantity', label: 'Verify amount or quantity' },
  { key: 'confirm_supplier_information', label: 'Confirm supplier information' },
  { key: 'other', label: 'Other' },
];

export function emptySendBackForm(): SendBackFormState {
  return {
    reasonCategory: '', reasonOtherNote: '', instruction: '',
    requiredActions: [], requiredActionOtherNote: '', priority: 'normal', dueDate: '',
  };
}

// Client-side mirror of helpers/send_back.py::validate_send_back_payload —
// instant feedback before the network round-trip; the backend re-
// validates the same rules and remains authoritative. Exported as a pure
// function (no DOM/HttpClient) so it's directly unit-testable.
export function validateSendBackForm(form: SendBackFormState, todayIso: string): string[] {
  const errors: string[] = [];
  if (!form.reasonCategory) errors.push('Please select a return reason category.');
  if (form.reasonCategory === 'other' && !form.reasonOtherNote.trim()) {
    errors.push('Please describe the "Other" reason.');
  }
  if (!form.instruction.trim()) errors.push('Auditor instruction is required.');
  if (form.requiredActions.length === 0) errors.push('Select at least one required action.');
  if (form.requiredActions.includes('other') && !form.requiredActionOtherNote.trim()) {
    errors.push('Please describe the "Other" required action.');
  }
  if (form.dueDate && form.dueDate < todayIso) {
    errors.push('Due date cannot be earlier than today.');
  }
  if (form.priority === 'high' && !form.dueDate) {
    errors.push('A response due date is required for high-priority send-back requests.');
  }
  return errors;
}

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

  // ── Send-Back workflow (Features 1, 4, 5) ──
  reasonCategoryOptions = REASON_CATEGORY_OPTIONS;
  requiredActionOptions = REQUIRED_ACTION_OPTIONS;
  showSendBackModal: boolean = false;
  sendBack: SendBackFormState = emptySendBackForm();
  sendBackErrors: string[] = [];
  cycles: any[] = [];
  reviewHistory: any[] = [];

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
        this.loadCycles();
        this.loadReviewHistory();
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
    if (source === 'phone_photo') return 'ph-device-mobile-camera';
    if (source === 'scanned') return 'ph-printer';
    if (source === 'digital_native') return 'ph-desktop';
    if (source === 'webcam') return 'ph-webcam';
    return 'ph-question';
  }

  authenticitySourceLabel(): string {
    const source = this.authenticity?.upload_source;
    if (source === 'phone_photo') return 'Phone Photo';
    if (source === 'scanned') return 'Scanned';
    if (source === 'digital_native') return 'Digital Native';
    if (source === 'webcam') return 'Webcam';
    return 'Unknown';
  }

  // ── Send-Back cycles + review history (Features 4, 5) ───
  // Two separate, deliberately UN-merged data sources:
  //   - cycles: the structured send-back detail (reason/instruction/
  //     required actions/priority/due date/Finance response) — powers
  //     the "Finance Response" + "Changes Since Send Back" panels.
  //   - reviewHistory: review_records, the EXISTING audit-log system
  //     (see helpers/audit_log.py / routes/reviews.py) — already has
  //     everything the History timeline needs (action, remarks,
  //     reviewer, timestamp), so it's used as-is rather than building a
  //     second competing log.

  loadCycles() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/reviews/send-back-cycles/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => { this.cycles = res.cycles || []; this.cdr.detectChanges(); },
      error: () => { this.cycles = []; }
    });
  }

  loadReviewHistory() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/reviews/history/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => { this.reviewHistory = res.history || []; this.cdr.detectChanges(); },
      error: () => { this.reviewHistory = []; }
    });
  }

  get latestCycle(): any {
    return this.cycles.length ? this.cycles[this.cycles.length - 1] : null;
  }

  get hasFinanceResponse(): boolean {
    return !!this.latestCycle?.finance_response;
  }

  get changesSinceSendBack(): string[] {
    return this.latestCycle?.activity_summary || [];
  }

  reasonCategoryLabel(key: string): string {
    return REASON_CATEGORY_OPTIONS.find(o => o.key === key)?.label || key;
  }

  requiredActionLabel(key: string): string {
    return REQUIRED_ACTION_OPTIONS.find(o => o.key === key)?.label || key;
  }

  priorityLabel(p: string): string {
    if (p === 'high') return 'High';
    if (p === 'medium') return 'Medium';
    return 'Normal';
  }

  priorityClass(p: string): string {
    if (p === 'high') return 'priority-high';
    if (p === 'medium') return 'priority-medium';
    return 'priority-normal';
  }

  historyLabel(action: string): string {
    if (action === 'returned') return 'Record sent back to Finance';
    if (action === 'resubmitted') return 'Record resubmitted for auditor review';
    if (action === 'approved') return 'Record approved';
    if (action === 'need_review') return 'Marked for further review';
    return action;
  }

  formatDateTime(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'
    });
  }

  // ── Send-Back modal (Feature 1) ──────────────────────────

  get todayIso(): string {
    return new Date().toISOString().slice(0, 10);
  }

  get sendBackButtonLabel(): string {
    return this.cycles.length > 0 ? 'Send Back Again' : 'Send Back to Finance';
  }

  openSendBackModal() {
    this.sendBack = emptySendBackForm();
    this.sendBackErrors = [];
    this.showSendBackModal = true;
  }

  closeSendBackModal() {
    this.showSendBackModal = false;
  }

  toggleRequiredAction(key: RequiredAction) {
    const i = this.sendBack.requiredActions.indexOf(key);
    if (i === -1) this.sendBack.requiredActions.push(key);
    else this.sendBack.requiredActions.splice(i, 1);
  }

  isRequiredActionChecked(key: RequiredAction): boolean {
    return this.sendBack.requiredActions.includes(key);
  }

  submitSendBack() {
    if (!this.documentId || this.isSubmitting) return;

    const errors = validateSendBackForm(this.sendBack, this.todayIso);
    if (errors.length) {
      this.sendBackErrors = errors;
      this.cdr.detectChanges();
      return;
    }
    this.sendBackErrors = [];
    this.isSubmitting = true;

    const payload: any = {
      reason_category: this.sendBack.reasonCategory,
      instruction: this.sendBack.instruction.trim(),
      required_actions: this.sendBack.requiredActions,
      priority: this.sendBack.priority,
    };
    if (this.sendBack.reasonOtherNote.trim()) payload.reason_other_note = this.sendBack.reasonOtherNote.trim();
    if (this.sendBack.requiredActionOtherNote.trim()) {
      payload.required_action_other_note = this.sendBack.requiredActionOtherNote.trim();
    }
    if (this.sendBack.dueDate) payload.due_date = this.sendBack.dueDate;

    this.http.post<any>(`${this.apiUrl}/reviews/return/${this.documentId}`,
      payload,
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.showSendBackModal = false;
        this.successMessage = 'Document returned to Finance!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/auditor/home']);
        }, 2000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.sendBackErrors = [err.error?.error || 'Failed to send back.'];
        this.cdr.detectChanges();
      }
    });
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
    // REVIEW reuses the same amber styling as PARTIAL (banner-partial) —
    // both are "needs attention, not a hard failure" states; only the
    // text differs (see getBannerText/getBannerSubtitle).
    return 'banner-partial';
  }

  getBannerIcon(): string {
    if (this.overallStatus === 'PASS') return 'ph-check-circle';
    if (this.overallStatus === 'FAIL') return 'ph-x-circle';
    return 'ph-warning';
  }

  getBannerText(): string {
    if (this.overallStatus === 'PASS') return 'All Fields Match';
    if (this.overallStatus === 'FAIL') return 'Mismatch Detected';
    if (this.overallStatus === 'REVIEW') return 'Review Required';
    return 'Documents Incomplete';
  }

  getBannerSubtitle(): string {
    if (this.overallStatus === 'PASS') return 'Ready for approval';
    if (this.overallStatus === 'FAIL') return 'Review required — see highlighted rows';
    if (this.overallStatus === 'REVIEW') return 'Some fields differ — see highlighted rows';
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

  amountSymbol(fromVal: number | null, toVal: number | null, fromCurrency?: string | null, toCurrency?: string | null): 'eq' | 'neq' | 'na' {
    if (fromVal === null || fromVal === undefined || toVal === null || toVal === undefined) return 'na';
    // Different known currencies (e.g. invoice in USD, PO in RM) make a
    // raw numeric comparison meaningless — treat as not-applicable
    // rather than silently comparing USD against RM as the same unit.
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
    if (match === true) return 'ph-check';
    if (match === false) return 'ph-warning';
    return 'ph-minus';
  }

  rowMatchText(match: boolean | null): string {
    if (match === true) return 'Match';
    if (match === false) return 'Mismatch';
    return 'N/A';
  }

  // ── Line Items (per-item, one row per matched item across Invoice/PO/
  // GR by item_code or normalized description — server-computed in
  // comparison.line_items) ──────────────────────────────────

  private lineItemMissing(li: any, side: 'po' | 'gr'): boolean {
    return side === 'po' ? !!li.missing_on_po : !!li.missing_on_gr;
  }

  lineItemRowClass(li: any): string[] {
    const hardIssue = li.quantity_match === false || li.missing_on_invoice || li.missing_on_po || li.missing_on_gr;
    if (hardIssue) return ['row-mismatch', 'row-quantity-alert'];
    // amount_match is a SOFT check (drives the amber REVIEW banner state,
    // never red FAIL) — still needs a visible row indicator, or it would
    // be an invisible check silently affecting the banner with nothing
    // in the table for an auditor to actually see (the exact bug fixed
    // for date_order_valid/po_reference_match in earlier work on this
    // page). Standard row-mismatch styling only, no row-quantity-alert
    // stripe — that's reserved for the hard quantity case.
    if (li.amount_match === false) return ['row-mismatch'];
    return [];
  }

  lineItemPillClass(li: any, side: 'po' | 'gr'): string {
    if (this.lineItemMissing(li, side)) return 'pill-differ';
    return this.rowMatchPillClass(li.quantity_match);
  }

  lineItemPillIcon(li: any, side: 'po' | 'gr'): string {
    if (this.lineItemMissing(li, side)) return 'ph-warning';
    return this.rowMatchIcon(li.quantity_match);
  }

  lineItemPillText(li: any, side: 'po' | 'gr'): string {
    if (side === 'po' && li.missing_on_po) return 'Missing on PO';
    if (side === 'gr' && li.missing_on_gr) return 'Missing on GR';
    return this.rowMatchText(li.quantity_match);
  }

  formatQuantity(qty: any): string {
    if (qty === null || qty === undefined || qty === '') return '-';
    const n = parseFloat(qty);
    return Number.isInteger(n) ? String(n) : n.toFixed(2);
  }

  // ── Formatting ───────────────────────────────────────────

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
