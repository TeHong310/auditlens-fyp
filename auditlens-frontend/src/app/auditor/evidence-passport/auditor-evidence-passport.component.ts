import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';
import { formatMalaysiaDateTime } from '../../shared/datetime.util';

// Audit Evidence Passport — one read-only, exportable summary of a
// single transaction, reached from the "View Evidence Passport"/
// "Export PDF" buttons on the new Passport card on Audit Review
// (Record Detail). Every value comes from GET /documents/<id>/
// evidence-passport, which itself is assembled entirely from existing
// helpers (_build_case_context, build_comparison,
// get_transaction_context_for_document, the same document_review_steps/
// send_back_cycles queries the Timeline/Send-Back-History endpoints
// already use) plus one new piece: document integrity (SHA-256 baseline
// vs. recomputed hash). No matching/anomaly/authenticity/review-step
// logic lives here — purely presentation.
@Component({
  selector: 'app-auditor-evidence-passport',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-evidence-passport.component.html',
  styleUrls: ['./auditor-evidence-passport.component.css']
})
export class AuditorEvidencePassportComponent implements OnInit {

  documentId: number | null = null;
  passport: any = null;
  isLoading: boolean = false;
  errorMessage: string = '';

  // Set when reached via the Passport card's [Export PDF] button
  // (?export=1) — triggers window.print() once automatically after the
  // data finishes loading, instead of requiring a second click on this
  // page's own Export PDF button.
  private autoExport: boolean = false;

  // Header "Generated" timestamp — set once, at the moment this
  // Passport view actually loaded, not a stored server value.
  generatedAt: string = '';

  private static REVIEW_STEP_ORDER = ['three_way_matching', 'exception_review', 'authenticity_review', 'anomaly_review'];

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private route: ActivatedRoute,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.autoExport = this.route.snapshot.queryParamMap.get('export') === '1';
    this.route.paramMap.subscribe(params => {
      const id = params.get('document_id');
      if (id) {
        this.documentId = parseInt(id, 10);
        this.loadPassport();
      }
    });
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // Returns to THIS record's Audit Review page — same pattern as every
  // other case-specific detail page (Matching Details, Authenticity
  // Detail), preserving transaction_package_id when this invoice
  // belongs to one.
  backToAuditReview() {
    if (!this.documentId) return;
    const queryParams: any = { document_id: this.documentId };
    const pkgId = this.passport?.transaction_context?.transaction_package_id;
    if (pkgId) queryParams.transaction_package_id = pkgId;
    this.router.navigate(['/auditor/record-detail'], { queryParams });
  }

  loadPassport() {
    if (!this.documentId) return;
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/documents/${this.documentId}/evidence-passport`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.passport = res;
        this.generatedAt = new Date().toISOString();
        this.isLoading = false;
        this.cdr.detectChanges();
        if (this.autoExport) {
          this.autoExport = false;
          // Let the DOM actually paint the just-loaded sections before
          // the browser's print dialog captures them.
          setTimeout(() => window.print(), 300);
        }
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load evidence passport.';
        this.cdr.detectChanges();
      }
    });
  }

  exportPdf() {
    window.print();
  }

  // ── Display helpers — formatAmount/formatDate copied verbatim from
  // auditor-matching-details.component.ts (same small-per-page-helper
  // convention already used across this app, not a new shared util);
  // formatDateTime/historyLabel copied verbatim from
  // auditor-record-detail.component.ts for the same reason. ──

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

  formatDateTime(dateStr: string): string {
    return formatMalaysiaDateTime(dateStr);
  }

  historyLabel(action: string): string {
    if (action === 'returned') return 'Record sent back to Finance';
    if (action === 'resubmitted') return 'Record resubmitted for auditor review';
    if (action === 'approved') return 'Record approved';
    if (action === 'need_review') return 'Marked for further review';
    if (action === 'closed') return 'Correction case closed';
    return action;
  }

  // Final Audit Decision (Section 6/7) — only the LAST entry, and only
  // when it's a terminal action (approved/returned), is framed as the
  // case's actual final decision; an earlier need_review/returned later
  // superseded by an Approve keeps its own plain wording.
  decisionEntryLabel(action: string, isLast: boolean): string {
    if (isLast && action === 'approved') return 'Final Audit Decision: Approved';
    if (isLast && action === 'returned') return 'Final Audit Decision: Sent Back to Finance';
    return this.historyLabel(action);
  }

  reviewStepLabel(step: string): string {
    if (step === 'three_way_matching') return 'Three-Way Matching Review';
    if (step === 'exception_review') return 'Exception Review';
    if (step === 'authenticity_review') return 'Authenticity Review';
    if (step === 'anomaly_review') return 'Anomaly Review';
    return step;
  }

  integrityStatusLabel(status: string): string {
    if (status === 'verified') return 'Verified';
    if (status === 'warning') return 'Warning';
    if (status === 'not_recorded') return 'Baseline Not Available';
    return 'Not Applicable';
  }

  docTypeLabel(type: string): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'Purchase Order';
    return 'Goods Receipt';
  }

  objectKeys(obj: any): string[] {
    return obj ? Object.keys(obj) : [];
  }

  // review_steps arrives as an object keyed by step — always rendered
  // in the fixed workflow order (Three-Way Matching -> Exception ->
  // Authenticity -> Anomaly), not whatever order the DB happened to
  // return rows in.
  get reviewStepEntries(): { step: string; data: any }[] {
    const steps = this.passport?.review_steps || {};
    return AuditorEvidencePassportComponent.REVIEW_STEP_ORDER
      .filter(step => steps[step])
      .map(step => ({ step, data: steps[step] }));
  }

  // ── Passport header (Section 2) ──

  get passportId(): string {
    return this.documentId ? `EVP-${this.documentId}` : '-';
  }

  // Same latestReviewAction/caseFinalDecision derivation
  // auditor-record-detail.component.ts already uses for its own
  // Passport card — audit_history is ASC-ordered (last = most recent),
  // same convention as that page's reviewHistory. Purely a read of the
  // existing decision state; makes no decision itself.
  private get latestDecisionAction(): string | null {
    const history = this.passport?.audit_history || [];
    return history.length ? history[history.length - 1].action : null;
  }

  get passportStatus(): string {
    const action = this.latestDecisionAction;
    if (action === 'approved') return 'Finalised';
    if (action === 'returned') return 'Correction Required';
    if (action === 'need_review') return 'Further Review';
    return 'Draft';
  }

  get passportStatusClass(): string {
    const action = this.latestDecisionAction;
    if (action === 'approved') return 'passport-status-finalised';
    if (action === 'returned') return 'passport-status-correction';
    if (action === 'need_review') return 'passport-status-further-review';
    return 'passport-status-draft';
  }

  // ── Transaction Summary (Section 1) — package status derived from
  // which documents are ACTUALLY linked (comparison.po/gr presence),
  // not merely whether this invoice belongs to a formal Finance
  // Transaction Package — a standalone 3-way-matched invoice with a
  // real PO and GR attached is a Complete Transaction Package, not a
  // "standalone invoice" as the page used to (incorrectly) say. ──

  get transactionStatusLabel(): string {
    const hasPo = !!this.passport?.comparison?.po;
    const hasGr = !!this.passport?.comparison?.gr;
    if (hasPo && hasGr) return 'Complete Transaction Package';
    if (hasPo || hasGr) return 'Incomplete Transaction Package';
    return 'Standalone Invoice';
  }

  get transactionMissingNote(): string | null {
    const hasPo = !!this.passport?.comparison?.po;
    const hasGr = !!this.passport?.comparison?.gr;
    if (hasPo && !hasGr) return 'Missing: Goods Receipt';
    if (hasGr && !hasPo) return 'Missing: Purchase Order';
    return null;
  }

  get documentsIncludedLabel(): string {
    const included = ['Invoice'];
    if (this.passport?.comparison?.po) included.push('Purchase Order');
    if (this.passport?.comparison?.gr) included.push('Goods Receipt');
    return included.join(', ');
  }

  // ── Wording consistency (Section 7) — normalizes whatever casing the
  // underlying enum/status value happens to be stored in (some are
  // UPPERCASE like match_result.overall_status, some lowercase like
  // authenticity_status/anomaly severity) into one consistent Title
  // Case display, without needing to know each field's exact stored
  // casing. Does not change any stored value, only how it's shown. ──

  titleCase(value: string | null | undefined): string {
    if (!value) return '-';
    return value.toString().toLowerCase().replace(/(^|[\s_-])(\w)/g, (_m, sep, ch) => (sep === '_' || sep === '-' ? ' ' : sep) + ch.toUpperCase());
  }

  matchOverallLabel(status: string | null | undefined): string {
    if (status === 'PASS') return 'Passed';
    if (status === 'FAIL') return 'Failed';
    if (status === 'REVIEW') return 'Review Required';
    if (status === 'PARTIAL') return 'Partial Match';
    return this.titleCase(status);
  }
}
