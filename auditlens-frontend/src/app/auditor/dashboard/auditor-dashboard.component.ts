import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';

@Component({
  selector: 'app-auditor-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-dashboard.component.html',
  styleUrls: ['./auditor-dashboard.component.css']
})
export class AuditorDashboardComponent implements OnInit {

  isLoading: boolean = false;
  documents: any[] = [];

  // Stats
  totalRecords: number = 0;
  fullMatch: number = 0;
  needReview: number = 0;
  exceptions: number = 0;

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
        this.totalRecords = this.documents.length;
        this.needReview   = this.documents.filter((d: any) => d.status === 'under_review').length;
        this.fullMatch    = this.documents.filter((d: any) => d.status === 'approved').length;
        this.exceptions   = this.documents.filter((d: any) => !d.has_gr).length;
        this.isLoading    = false;
        this.cdr.detectChanges();
      },
      error: () => { this.isLoading = false; }
    });
  }

  goToReviewQueue() {
    this.router.navigate(['/auditor/review-queue']);
  }

  goToRecord(doc: any) {
    this.router.navigate(['/auditor/record-detail'], {
      queryParams: { document_id: doc.document_id }
    });
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

  getStatusLabel(status: string): string {
    switch (status) {
      case 'approved':     return 'Approved';
      case 'under_review': return 'Need Review';
      case 'returned':     return 'Returned';
      case 'resubmitted':  return 'Resubmitted';
      default:             return status;
    }
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  formatAmount(amount: any): string {
    if (!amount) return '-';
    return 'RM ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }
}