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

  reviewStepLabel(step: string): string {
    if (step === 'three_way_matching') return 'Three-Way Matching';
    if (step === 'exception_review') return 'Exception Review';
    if (step === 'authenticity_review') return 'Authenticity Review';
    if (step === 'anomaly_review') return 'Anomaly Review';
    return step;
  }

  integrityStatusLabel(status: string): string {
    if (status === 'verified') return 'Verified';
    if (status === 'warning') return 'Warning';
    if (status === 'not_recorded') return 'Not Yet Recorded';
    return 'Not Applicable';
  }

  docTypeLabel(type: string): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'Purchase Order';
    return 'Goods Receipt';
  }

  // review_steps / anomalies / send_back_cycles arrive as either an
  // object or array depending on section — small helpers so the
  // template can iterate consistently.
  get reviewStepEntries(): { step: string; data: any }[] {
    const steps = this.passport?.review_steps || {};
    return Object.keys(steps).map(step => ({ step, data: steps[step] }));
  }
}
