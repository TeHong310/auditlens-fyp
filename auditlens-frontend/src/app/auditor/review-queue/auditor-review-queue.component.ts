import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';

@Component({
  selector: 'app-auditor-review-queue',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-review-queue.component.html',
  styleUrls: ['./auditor-review-queue.component.css']
})
export class AuditorReviewQueueComponent implements OnInit {

  documents: any[] = [];
  filteredDocuments: any[] = [];
  isLoading: boolean = false;
  searchText: string = '';
  activeFilter: string = 'all';

  // Stats
  totalOpen: number = 0;
  highRisk: number = 0;
  lowOCR: number = 0;
  missingDocs: number = 0;

  private apiUrl = 'http://localhost:5000';

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadQueue();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadQueue() {
    this.isLoading = true;
    this.http.get<any>(`${this.apiUrl}/matching/queue`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.documents = res.documents || [];
        this.totalOpen    = this.documents.length;
        this.highRisk     = this.documents.filter((d: any) => !d.has_gr || !d.has_po).length;
        this.lowOCR       = this.documents.filter((d: any) => d.ocr_confidence && parseFloat(d.ocr_confidence) < 70).length;
        this.missingDocs  = this.documents.filter((d: any) => !d.has_gr).length;
        this.applyFilter();
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: () => { this.isLoading = false; }
    });
  }

  setFilter(filter: string) {
    this.activeFilter = filter;
    this.applyFilter();
  }

  applyFilter() {
    let result = [...this.documents];

    if (this.searchText) {
      const search = this.searchText.toLowerCase();
      result = result.filter(d =>
        d.invoice_number?.toLowerCase().includes(search) ||
        d.vendor_name?.toLowerCase().includes(search) ||
        d.file_name?.toLowerCase().includes(search)
      );
    }

    if (this.activeFilter === 'high_risk') {
      result = result.filter(d => !d.has_gr || !d.has_po);
    } else if (this.activeFilter === 'low_ocr') {
      result = result.filter(d => d.ocr_confidence && parseFloat(d.ocr_confidence) < 70);
    } else if (this.activeFilter === 'missing') {
      result = result.filter(d => !d.has_gr);
    }

    this.filteredDocuments = result;
  }

  openRecord(doc: any) {
    this.router.navigate(['/auditor/record-detail'], {
      queryParams: { document_id: doc.document_id }
    });
  }

  getPriority(doc: any): string {
    if (!doc.has_gr || !doc.has_po) return 'High';
    if (doc.ocr_confidence && parseFloat(doc.ocr_confidence) < 70) return 'Medium';
    return 'Low';
  }

  getPriorityClass(doc: any): string {
    const p = this.getPriority(doc);
    if (p === 'High')   return 'priority-high';
    if (p === 'Medium') return 'priority-medium';
    return 'priority-low';
  }

  getFlagLabel(doc: any): string {
    if (!doc.has_gr && !doc.has_po) return 'Missing PO & GR';
    if (!doc.has_gr) return 'Missing GR';
    if (!doc.has_po) return 'Missing PO';
    if (doc.ocr_confidence && parseFloat(doc.ocr_confidence) < 70) return 'Low OCR';
    return 'Pending Review';
  }

  getFlagClass(doc: any): string {
    const flag = this.getFlagLabel(doc);
    if (flag.includes('Missing')) return 'flag-missing';
    if (flag === 'Low OCR') return 'flag-low-ocr';
    return 'flag-pending';
  }

  getStatusClass(status: string): string {
    switch (status) {
      case 'approved':     return 'badge-approved';
      case 'under_review': return 'badge-review';
      case 'returned':     return 'badge-returned';
      case 'resubmitted':  return 'badge-resubmitted';
      default:             return 'badge-pending';
    }
  }

  formatAmount(amount: any): string {
    if (!amount) return '-';
    return 'RM ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }
  parseFloat(val: any): number {
  return parseFloat(val);
}
}