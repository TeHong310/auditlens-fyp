import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { forkJoin, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { environment } from '../../../environments/environment';

// Same machine-key -> label lookups as finance-ocr-review.component.ts
// and auditor-record-detail.component.ts — mirrors helpers/send_back.py
// exactly, kept as a separate copy per component per this codebase's
// established convention (no shared constants file for these).
const REASON_CATEGORY_LABELS: Record<string, string> = {
  missing_document: 'Missing document',
  incorrect_extracted_information: 'Incorrect extracted information',
  invoice_po_gr_mismatch: 'Invoice / PO / GR mismatch',
  possible_duplicate_invoice: 'Possible duplicate invoice',
  authenticity_evidence_requires_clarification: 'Authenticity evidence requires clarification',
  incorrect_supplier_information: 'Incorrect supplier information',
  amount_or_quantity_requires_verification: 'Amount or quantity requires verification',
  other: 'Other',
};

const REQUIRED_ACTION_LABELS: Record<string, string> = {
  upload_missing_document: 'Upload missing document',
  correct_extracted_information: 'Correct extracted information',
  provide_written_explanation: 'Provide written explanation',
  confirm_duplicate_submission: 'Confirm duplicate submission',
  replace_incorrect_document: 'Replace incorrect document',
  verify_amount_or_quantity: 'Verify amount or quantity',
  confirm_supplier_information: 'Confirm supplier information',
  other: 'Other',
};

@Component({
  selector: 'app-finance-corrections',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './finance-corrections.component.html',
  styleUrls: ['./finance-corrections.component.css']
})
export class FinanceCorrectionsComponent implements OnInit {
  isLoading: boolean = false;
  errorMessage: string = '';
  searchText: string = '';

  // One row per returned invoice, enriched with its latest send_back_
  // cycle (reason/priority/required actions/return date) — see
  // loadCorrections()/attachCycles() below for how it's built purely by
  // combining two EXISTING endpoints, no new backend code.
  corrections: any[] = [];

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    this.loadCorrections();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // Reuses GET /documents/ (already scoped to the current Finance
  // user's own uploads) — filters to status === 'returned' client-side,
  // exactly the same filter finance-ocr-review.component.ts's
  // returnedDocuments getter already applies, just as the ONLY thing
  // shown here instead of mixed in with the OCR review queue.
  loadCorrections() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/documents/`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        const returned = (res.documents || []).filter((d: any) => d.status === 'returned');
        this.attachCycles(returned);
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load returned invoices.';
        this.cdr.detectChanges();
      }
    });
  }

  // Reuses GET /reviews/send-back-cycles/<id> (already exists, already
  // used by finance-ocr-review.component.ts for the SAME purpose on a
  // single selected document) — fetched in parallel for every returned
  // invoice via forkJoin, the same pattern finance-ocr-review.component.
  // ts's loadRelatedDocsForAll() already uses for PO/GR status. No new
  // backend endpoint.
  private attachCycles(returnedDocs: any[]) {
    if (!returnedDocs.length) {
      this.corrections = [];
      this.isLoading = false;
      this.cdr.detectChanges();
      return;
    }

    const requests: { [documentId: number]: any } = {};
    for (const doc of returnedDocs) {
      requests[doc.document_id] = this.http.get<any>(
        `${this.apiUrl}/reviews/send-back-cycles/${doc.document_id}`,
        { headers: this.getHeaders() }
      ).pipe(catchError(() => of(null)));
    }

    forkJoin(requests).subscribe((results: any) => {
      this.corrections = returnedDocs.map(doc => {
        const cycles = results[doc.document_id]?.cycles || [];
        const latestCycle = cycles.length ? cycles[cycles.length - 1] : null;
        return { ...doc, latestCycle };
      });
      this.isLoading = false;
      this.cdr.detectChanges();
    });
  }

  get filteredCorrections() {
    if (!this.searchText) return this.corrections;
    const q = this.searchText.toLowerCase();
    return this.corrections.filter(d =>
      d.invoice_number?.toLowerCase().includes(q) ||
      d.vendor_name?.toLowerCase().includes(q)
    );
  }

  reasonCategoryLabel(key: string): string {
    return REASON_CATEGORY_LABELS[key] || key;
  }

  requiredActionLabel(key: string): string {
    return REQUIRED_ACTION_LABELS[key] || key;
  }

  returnReasonLabel(doc: any): string {
    if (doc.latestCycle?.return_reason_category) {
      return this.reasonCategoryLabel(doc.latestCycle.return_reason_category);
    }
    // Legacy return (before the structured send-back form existed) —
    // no cycle row, so fall back to the same generic label the
    // exception classifier (routes/auditor.py::_classify_exception)
    // already uses for a 'sent_back' exception with no remark.
    return 'Sent back to Finance';
  }

  requiredActionsSummary(doc: any): string {
    const actions = doc.latestCycle?.required_actions || [];
    if (!actions.length) return '-';
    return actions.map((a: string) => this.requiredActionLabel(a)).join(', ');
  }

  returnedDateLabel(doc: any): string {
    const dateStr = doc.latestCycle?.sent_back_at || doc.updated_at;
    return this.formatDate(dateStr);
  }

  priorityLabel(doc: any): string {
    const p = doc.latestCycle?.priority || 'normal';
    return p.charAt(0).toUpperCase() + p.slice(1);
  }

  priorityClass(doc: any): string {
    const p = doc.latestCycle?.priority || 'normal';
    return 'priority-' + p;
  }

  currentStatusLabel(doc: any): string {
    return doc.status === 'returned' ? 'Awaiting Finance Correction' : doc.status;
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  openCorrection(doc: any) {
    this.router.navigate(['/finance/corrections/detail'], { queryParams: { document_id: doc.document_id } });
  }
}
