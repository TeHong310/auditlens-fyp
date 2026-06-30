import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

@Component({
  selector: 'app-finance-ocr-review',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './finance-ocr-review.component.html',
  styleUrls: ['./finance-ocr-review.component.css']
})
export class FinanceOcrReviewComponent implements OnInit {
  activeTab: 'invoice' | 'po' | 'gr' = 'invoice';

  // Invoice
  documents: any[] = [];
  selectedDoc: any = null;
  isLoading: boolean = false;
  isSaving: boolean = false;
  isSubmitting: boolean = false;
  successMessage: string = '';
  errorMessage: string = '';
  searchText: string = '';
  editFields: any = {
    invoice_number: '', vendor_name: '',
    invoice_date: '', total_amount: '', tax_amount: ''
  };

  // PO
  poList: any[] = [];
  selectedPO: any = null;
  isLoadingPO: boolean = false;
  isSavingPO: boolean = false;
  searchPO: string = '';
  editPOFields: any = {
    po_number: '', vendor_name: '', po_date: '', total_amount: ''
  };

  // GR
  grList: any[] = [];
  selectedGR: any = null;
  isLoadingGR: boolean = false;
  isSavingGR: boolean = false;
  searchGR: string = '';
  editGRFields: any = {
    gr_number: '', vendor_name: '', receipt_date: '', total_amount: ''
  };

  successPO: string = '';
  errorPO: string = '';
  successGR: string = '';
  errorGR: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    this.loadDocuments();
    this.loadPOList();
    this.loadGRList();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  setTab(tab: 'invoice' | 'po' | 'gr') {
    this.activeTab = tab;
    this.selectedDoc = null;
    this.selectedPO = null;
    this.selectedGR = null;
    this.successMessage = '';
    this.errorMessage = '';
    this.successPO = '';
    this.errorPO = '';
    this.successGR = '';
    this.errorGR = '';
  }

  // ── INVOICE ──────────────────────────────────────────────

  loadDocuments() {
    this.isLoading = true;
    this.http.get<any>(`${this.apiUrl}/documents/`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.documents = res.documents.filter((d: any) =>
          d.status === 'ocr_done' || d.status === 'returned'
        );
        this.isLoading = false;
        this.cdr.detectChanges();
        this.loadGRList(); // ← 加这行
        this.loadPOList();
      },
      error: () => { this.isLoading = false; }
    });
  }

  get filteredDocuments() {
    if (!this.searchText) return this.documents;
    return this.documents.filter(d =>
      d.file_name?.toLowerCase().includes(this.searchText.toLowerCase()) ||
      d.invoice_number?.toLowerCase().includes(this.searchText.toLowerCase())
    );
  }

  selectDocument(doc: any) {
    this.selectedDoc = doc;
    this.editFields = {
      invoice_number: doc.invoice_number || '',
      vendor_name: doc.vendor_name || '',
      invoice_date: doc.invoice_date || '',
      total_amount: doc.total_amount || '',
      tax_amount: doc.tax_amount || ''
    };
    this.successMessage = '';
    this.errorMessage = '';
    this.cdr.detectChanges();
  }

  saveChanges() {
    if (!this.selectedDoc) return;
    this.isSaving = true;
    this.successMessage = '';
    this.errorMessage = '';

    this.http.put<any>(
      `${this.apiUrl}/documents/${this.selectedDoc.document_id}/update-fields`,
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
        const idx = this.documents.findIndex(d => d.document_id === this.selectedDoc.document_id);
        if (idx !== -1) {
          this.documents[idx] = { ...this.documents[idx], ...this.editFields };
          this.selectedDoc = this.documents[idx];
        }
        this.cdr.detectChanges();
        setTimeout(() => { this.successMessage = ''; this.cdr.detectChanges(); }, 3000);
      },
      error: (err) => {
        this.isSaving = false;
        this.errorMessage = err.error?.error || 'Failed to save.';
        this.cdr.detectChanges();
      }
    });
  }

  canSubmit(): boolean {
    return !!(this.editFields.invoice_number && this.editFields.vendor_name &&
      this.editFields.total_amount && this.editFields.invoice_date);
  }

  submitToAuditor() {
    if (!this.selectedDoc || !this.canSubmit()) {
      this.errorMessage = 'Please fill in all required fields before submitting.';
      this.cdr.detectChanges();
      return;
    }
    this.isSubmitting = true;
    const isReturned = this.selectedDoc.status === 'returned';
    const url = isReturned
      ? `${this.apiUrl}/reviews/resubmit/${this.selectedDoc.document_id}`
      : `${this.apiUrl}/reviews/submit/${this.selectedDoc.document_id}`;

    this.http.post<any>(url, {}, { headers: this.getHeaders() }).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = isReturned
          ? 'Document resubmitted to Auditor successfully!'
          : 'Document submitted to Auditor successfully!';
        this.documents = this.documents.filter(d => d.document_id !== this.selectedDoc.document_id);
        this.grList = this.grList.filter(
          g => g.document_id !== this.selectedDoc.document_id
        );
        this.poList = this.poList.filter(
          p => p.document_id !== this.selectedDoc.document_id
        );
        this.selectedDoc = null;
        this.cdr.detectChanges();
        setTimeout(() => { this.successMessage = ''; this.cdr.detectChanges(); }, 4000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.errorMessage = err.error?.error || 'Failed to submit.';
        this.cdr.detectChanges();
      }
    });
  }

  viewDocument(doc: any) {
    const token = localStorage.getItem('access_token');
    const url = `${this.apiUrl}/documents/${doc.document_id}/file`;
    fetch(url, { headers: { 'Authorization': `Bearer ${token}` } })
      .then(res => res.blob())
      .then(blob => window.open(URL.createObjectURL(blob), '_blank'))
      .catch(() => { this.errorMessage = 'Failed to open file.'; this.cdr.detectChanges(); });
  }

  deleteDocument(doc: any) {
    if (!confirm(`Delete "${doc.file_name}"? This cannot be undone.`)) return;

    const token = localStorage.getItem('access_token');
    this.http.delete<any>(`${this.apiUrl}/documents/${doc.document_id}`, {
      headers: new HttpHeaders({ 'Authorization': `Bearer ${token}` })
    }).subscribe({
      next: () => {
        this.documents = this.documents.filter(d => d.document_id !== doc.document_id);
        if (this.selectedDoc?.document_id === doc.document_id) {
          this.selectedDoc = null;
        }
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.errorMessage = err.error?.error || 'Failed to delete.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── PO ───────────────────────────────────────────────────

  loadPOList() {
    this.isLoadingPO = true;
    this.http.get<any>(`${this.apiUrl}/documents/po/list`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.poList = res.purchase_orders || [];
        this.isLoadingPO = false;
        this.cdr.detectChanges();
      },
      error: () => { this.isLoadingPO = false; }
    });
  }

  get filteredPO() {
    if (!this.searchPO) return this.poList;
    return this.poList.filter(p =>
      p.file_name?.toLowerCase().includes(this.searchPO.toLowerCase()) ||
      p.po_number?.toLowerCase().includes(this.searchPO.toLowerCase())
    );
  }

  selectPO(po: any) {
    this.selectedPO = po;
    this.editPOFields = {
      po_number: po.po_number || '',
      vendor_name: po.vendor_name || '',
      po_date: po.po_date || '',
      total_amount: po.total_amount || ''
    };
    this.successPO = '';
    this.errorPO = '';
    this.cdr.detectChanges();
  }

  savePO() {
    if (!this.selectedPO) return;
    this.isSavingPO = true;
    this.successPO = '';
    this.errorPO = '';

    this.http.put<any>(
      `${this.apiUrl}/documents/po/${this.selectedPO.po_id}/update`,
      this.editPOFields,
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSavingPO = false;
        this.successPO = 'PO saved successfully!';
        const idx = this.poList.findIndex(p => p.po_id === this.selectedPO.po_id);
        if (idx !== -1) {
          this.poList[idx] = { ...this.poList[idx], ...this.editPOFields };
          this.selectedPO = this.poList[idx];
        }
        this.cdr.detectChanges();
        setTimeout(() => { this.successPO = ''; this.cdr.detectChanges(); }, 3000);
      },
      error: (err) => {
        this.isSavingPO = false;
        this.errorPO = err.error?.error || 'Failed to save PO.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── GR ───────────────────────────────────────────────────

  loadGRList() {
    this.isLoadingGR = true;
    this.http.get<any>(`${this.apiUrl}/documents/gr/list`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        const allGR = res.goods_receipts || [];
        // Only show GR whose invoice is still ocr_done or returned
        const pendingDocIds = this.documents.map((d: any) => d.document_id);
        this.grList = allGR.filter((g: any) =>
          pendingDocIds.includes(g.document_id)
        );
        this.isLoadingGR = false;
        this.cdr.detectChanges();
      },
      error: () => { this.isLoadingGR = false; }
    });
  }

  get filteredGR() {
    if (!this.searchGR) return this.grList;
    return this.grList.filter(g =>
      g.file_name?.toLowerCase().includes(this.searchGR.toLowerCase()) ||
      g.gr_number?.toLowerCase().includes(this.searchGR.toLowerCase())
    );
  }

  selectGR(gr: any) {
    this.selectedGR = gr;
    this.editGRFields = {
      gr_number: gr.gr_number || '',
      vendor_name: gr.vendor_name || '',
      receipt_date: gr.receipt_date || '',
      total_amount: gr.total_amount || ''
    };
    this.successGR = '';
    this.errorGR = '';
    this.cdr.detectChanges();
  }

  saveGR() {
    if (!this.selectedGR) return;
    this.isSavingGR = true;
    this.successGR = '';
    this.errorGR = '';

    this.http.put<any>(
      `${this.apiUrl}/documents/gr/${this.selectedGR.gr_id}/update`,
      this.editGRFields,
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSavingGR = false;
        this.successGR = 'GR saved successfully!';
        const idx = this.grList.findIndex(g => g.gr_id === this.selectedGR.gr_id);
        if (idx !== -1) {
          this.grList[idx] = { ...this.grList[idx], ...this.editGRFields };
          this.selectedGR = this.grList[idx];
        }
        this.cdr.detectChanges();
        setTimeout(() => { this.successGR = ''; this.cdr.detectChanges(); }, 3000);
      },
      error: (err) => {
        this.isSavingGR = false;
        this.errorGR = err.error?.error || 'Failed to save GR.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Shared helpers ────────────────────────────────────────

  getConfidenceClass(confidence: number): string {
    if (confidence >= 80) return 'confidence-high';
    if (confidence >= 60) return 'confidence-medium';
    return 'confidence-low';
  }

  getConfidenceLabel(confidence: number): string {
    if (confidence >= 80) return 'High';
    if (confidence >= 60) return 'Medium';
    return 'Low';
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  getErrorType(doc: any): string {
    if (!doc.invoice_number) return 'Missing Invoice No';
    if (!doc.vendor_name) return 'Missing Vendor';
    if (!doc.total_amount) return 'Missing Amount';
    if (parseFloat(doc.ocr_confidence) < 60) return 'Low OCR Confidence';
    if (doc.status === 'returned') return 'Returned';
    return 'Ready';
  }

  getErrorClass(doc: any): string {
    const err = this.getErrorType(doc);
    if (err === 'Ready') return 'badge-ready';
    if (err === 'Returned') return 'badge-returned';
    return 'badge-error';
  }
}