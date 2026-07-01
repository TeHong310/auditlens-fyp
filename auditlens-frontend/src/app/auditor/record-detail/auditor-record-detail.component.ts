import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

@Component({
  selector: 'app-auditor-record-detail',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-record-detail.component.html',
  styleUrls: ['./auditor-record-detail.component.css']
})
export class AuditorRecordDetailComponent implements OnInit {

  documentId: number | null = null;
  document: any = null;
  matchResult: any = null;
  supporting: any = null;
  exceptions: any[] = [];
  isLoading: boolean = false;
  isRunningMatch: boolean = false;
  isSubmitting: boolean = false;
  successMessage: string = '';
  errorMessage: string = '';
  auditNote: string = '';
  matchType: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private route: ActivatedRoute,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.route.queryParams.subscribe(params => {
      if (params['document_id']) {
        this.documentId = parseInt(params['document_id']);
        this.loadDocument();
        this.loadMatchResult();
        this.loadExceptions();
        this.loadSupporting();
      }
    });
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadDocument() {
    if (!this.documentId) return;
    this.isLoading = true;
    this.http.get<any>(`${this.apiUrl}/documents/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.document = res.document;
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: () => { this.isLoading = false; }
    });
  }

  loadMatchResult() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/matching/result/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.matchResult = res.match_result;
        this.matchType = this.matchResult?.po_id ? '3-Way' : '2-Way';
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  loadExceptions() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/matching/exceptions/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.exceptions = res.exceptions || [];
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  loadSupporting() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/documents/${this.documentId}/supporting`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.supporting = res;
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  runMatching() {
    if (!this.documentId) return;
    this.isRunningMatch = true;
    this.errorMessage = '';
    this.successMessage = '';

    this.http.post<any>(`${this.apiUrl}/matching/run/${this.documentId}`, {}, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.isRunningMatch = false;
        this.matchResult = res;
        this.matchType = res.match_type === '3-way' ? '3-Way' : '2-Way';
        this.successMessage = `${res.match_type} matching completed! Score: ${res.overall_score}%`;
        this.loadExceptions();
        this.loadDocument();
        this.cdr.detectChanges();
        setTimeout(() => { this.successMessage = ''; this.cdr.detectChanges(); }, 4000);
      },
      error: (err) => {
        this.isRunningMatch = false;
        this.errorMessage = err.error?.error || 'Matching failed.';
        this.cdr.detectChanges();
      }
    });
  }

  approveDocument() {
    if (!this.documentId) return;
    this.isSubmitting = true;
    this.http.post<any>(`${this.apiUrl}/reviews/approve/${this.documentId}`,
      { note: this.auditNote },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = 'Document approved successfully!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/auditor/review-queue']);
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
      { note: this.auditNote },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = 'Document returned to Finance!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/auditor/review-queue']);
        }, 2000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.errorMessage = err.error?.error || 'Failed to return.';
        this.cdr.detectChanges();
      }
    });
  }

  getMatchClass(matched: boolean | null): string {
    if (matched === null || matched === undefined) return 'match-na';
    return matched ? 'match-yes' : 'match-no';
  }

  getMatchLabel(matched: boolean | null): string {
    if (matched === null || matched === undefined) return 'N/A';
    return matched ? 'Match' : 'Mismatch';
  }

  getOverallClass(status: string): string {
    if (status === 'full_match') return 'status-full';
    if (status === 'partial_match') return 'status-partial';
    return 'status-mismatch';
  }

  getSeverityClass(severity: string): string {
    if (severity === 'high')   return 'sev-high';
    if (severity === 'medium') return 'sev-medium';
    return 'sev-low';
  }

  formatAmount(amount: any): string {
    if (!amount) return '-';
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

  goBack() {
    this.router.navigate(['/auditor/review-queue']);
  }
}