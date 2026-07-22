import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

// Same lookups as finance-corrections.component.ts / finance-ocr-
// review.component.ts / auditor-record-detail.component.ts.
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
  selector: 'app-finance-correction-detail',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './finance-correction-detail.component.html',
  styleUrls: ['./finance-correction-detail.component.css']
})
export class FinanceCorrectionDetailComponent implements OnInit {

  documentId: number | null = null;
  document: any = null;
  latestCycle: any = null;
  relatedDocs: any = null;

  isLoading: boolean = false;
  isSaving: boolean = false;
  isSubmitting: boolean = false;
  isUploadingPO: boolean = false;
  isUploadingGR: boolean = false;
  successMessage: string = '';
  errorMessage: string = '';
  poMessage: string = '';
  grMessage: string = '';

  editFields: any = {
    invoice_number: '', vendor_name: '', invoice_date: '', total_amount: '', tax_amount: ''
  };
  financeResponse: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private route: ActivatedRoute,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    this.route.queryParams.subscribe(params => {
      if (params['document_id']) {
        this.documentId = parseInt(params['document_id']);
        this.loadAll();
      }
    });
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // Reuses GET /documents/<id> (already exists — same single-document
  // detail endpoint the rest of the app uses) for Original Invoice
  // Information, GET /reviews/send-back-cycles/<id> for the Auditor
  // Request panel, and GET /ocr-review/invoice/<id>/related-docs for
  // PO/GR upload status — all three already power finance-ocr-review.
  // component.ts / finance-upload.component.ts today. No new backend.
  loadAll() {
    if (!this.documentId) return;
    this.isLoading = true;
    this.errorMessage = '';

    this.http.get<any>(`${this.apiUrl}/documents/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.document = res.document;
        this.editFields = {
          invoice_number: this.document.invoice_number || '',
          vendor_name: this.document.vendor_name || '',
          invoice_date: this.document.invoice_date || '',
          total_amount: this.document.total_amount || '',
          tax_amount: this.document.tax_amount || ''
        };
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load invoice.';
        this.cdr.detectChanges();
      }
    });

    this.loadCycle();
    this.loadRelatedDocs();
  }

  loadCycle() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/reviews/send-back-cycles/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        const cycles = res.cycles || [];
        this.latestCycle = cycles.length ? cycles[cycles.length - 1] : null;
        this.cdr.detectChanges();
      },
      error: () => { this.latestCycle = null; }
    });
  }

  loadRelatedDocs() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/ocr-review/invoice/${this.documentId}/related-docs`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => { this.relatedDocs = res; this.cdr.detectChanges(); },
      error: () => { this.relatedDocs = null; }
    });
  }

  reasonCategoryLabel(key: string): string {
    return REASON_CATEGORY_LABELS[key] || key;
  }

  requiredActionLabel(key: string): string {
    return REQUIRED_ACTION_LABELS[key] || key;
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  formatAmount(amount: any, currency?: string | null): string {
    if (amount === null || amount === undefined || amount === '') return '-';
    return (currency || 'RM') + ' ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  // ── Save Changes — reuses the EXACT same PUT /documents/<id>/update-
  // fields endpoint finance-ocr-review.component.ts already calls. ──
  saveChanges() {
    if (!this.documentId) return;
    this.isSaving = true;
    this.successMessage = '';
    this.errorMessage = '';

    this.http.put<any>(
      `${this.apiUrl}/documents/${this.documentId}/update-fields`,
      {
        invoice_number: this.editFields.invoice_number,
        vendor_name: this.editFields.vendor_name,
        invoice_date: this.editFields.invoice_date,
        total_amount: this.editFields.total_amount || null,
        tax_amount: this.editFields.tax_amount || null,
      },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSaving = false;
        this.successMessage = 'Changes saved successfully!';
        this.document = { ...this.document, ...this.editFields };
        this.cdr.detectChanges();
        setTimeout(() => { this.successMessage = ''; this.cdr.detectChanges(); }, 3000);
      },
      error: (err) => {
        this.isSaving = false;
        this.errorMessage = err.error?.error || 'Failed to save changes.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Upload supporting documents — reuses the EXACT same POST
  // /documents/upload-po/<id> and /upload-gr/<id> endpoints already
  // used by finance-upload.component.ts. ──

  onPOFileSelected(event: any) {
    const file = event.target.files[0];
    if (!file || !this.documentId) return;
    this.uploadPO(file);
    event.target.value = '';
  }

  onGRFileSelected(event: any) {
    const file = event.target.files[0];
    if (!file || !this.documentId) return;
    this.uploadGR(file);
    event.target.value = '';
  }

  uploadPO(file: File) {
    this.isUploadingPO = true;
    this.poMessage = '';
    const formData = new FormData();
    formData.append('document', file);

    this.http.post<any>(
      `${this.apiUrl}/documents/upload-po/${this.documentId}`,
      formData,
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.isUploadingPO = false;
        this.poMessage = `PO uploaded! PO Number: ${res.extracted_fields?.po_number || 'N/A'}`;
        this.loadRelatedDocs();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isUploadingPO = false;
        this.poMessage = err.error?.error || 'PO upload failed';
        this.cdr.detectChanges();
      }
    });
  }

  uploadGR(file: File) {
    this.isUploadingGR = true;
    this.grMessage = '';
    const formData = new FormData();
    formData.append('document', file);

    this.http.post<any>(
      `${this.apiUrl}/documents/upload-gr/${this.documentId}`,
      formData,
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.isUploadingGR = false;
        this.grMessage = `GR uploaded! GR Number: ${res.extracted_fields?.gr_number || 'N/A'}`;
        this.loadRelatedDocs();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isUploadingGR = false;
        this.grMessage = err.error?.error || 'GR upload failed';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Resubmit — reuses the EXACT same POST /reviews/resubmit/<id>
  // endpoint (and payload shape) finance-ocr-review.component.ts
  // already calls for a returned document. Requires a Finance response,
  // same validation rule already enforced there and, server-side, by
  // helpers/send_back.py::validate_finance_response_payload. ──

  canResubmit(): boolean {
    return !!this.financeResponse.trim();
  }

  resubmit() {
    if (!this.documentId || !this.canResubmit()) {
      this.errorMessage = 'Please add a Finance response before resubmitting.';
      this.cdr.detectChanges();
      return;
    }
    this.isSubmitting = true;
    this.errorMessage = '';

    this.http.post<any>(
      `${this.apiUrl}/reviews/resubmit/${this.documentId}`,
      { response: this.financeResponse.trim() },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = 'Invoice resubmitted to Auditor successfully!';
        this.cdr.detectChanges();
        setTimeout(() => this.router.navigate(['/finance/corrections']), 1500);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.errorMessage = err.error?.error || 'Failed to resubmit.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── File viewers — reuse the same blob-fetch pattern already used in
  // finance-ocr-review.component.ts's viewDocument(). ──

  private openBlob(url: string) {
    const token = localStorage.getItem('access_token');
    fetch(url, { headers: { 'Authorization': `Bearer ${token}` } })
      .then(res => { if (!res.ok) throw new Error('Failed'); return res.blob(); })
      .then(blob => window.open(URL.createObjectURL(blob), '_blank'))
      .catch(() => { this.errorMessage = 'Failed to open file.'; this.cdr.detectChanges(); });
  }

  viewDocument() {
    if (!this.documentId) return;
    this.openBlob(`${this.apiUrl}/documents/${this.documentId}/file`);
  }

  viewPO() {
    if (!this.relatedDocs?.po?.po_id) return;
    this.openBlob(`${this.apiUrl}/documents/po/${this.relatedDocs.po.po_id}/file`);
  }

  viewGR() {
    if (!this.relatedDocs?.gr?.gr_id) return;
    this.openBlob(`${this.apiUrl}/documents/gr/${this.relatedDocs.gr.gr_id}/file`);
  }

  goBack() {
    this.router.navigate(['/finance/corrections']);
  }
}
