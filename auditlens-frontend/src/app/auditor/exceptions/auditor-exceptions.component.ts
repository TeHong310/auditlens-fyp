import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type ExceptionType = 'all' | 'mismatch' | 'missing_document' | 'low_confidence' | 'sent_back';

@Component({
  selector: 'app-auditor-exceptions',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-exceptions.component.html',
  styleUrls: ['./auditor-exceptions.component.css']
})
export class AuditorExceptionsComponent implements OnInit {

  exceptions: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';
  activeFilter: ExceptionType = 'all';

  // Set ONLY when this page is reached from a specific case's Audit
  // Review Timeline (?document_id=X&ref=audit-review) — the normal
  // sidebar-accessed global list (no query params) leaves this null
  // and behaves exactly as before: every exception, no back button.
  caseDocumentId: number | null = null;

  filters: { key: ExceptionType; label: string }[] = [
    { key: 'all', label: 'All' },
    { key: 'mismatch', label: 'Mismatch' },
    { key: 'missing_document', label: 'Missing' },
    { key: 'low_confidence', label: 'Low Conf' },
    { key: 'sent_back', label: 'Sent Back' },
  ];

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private route: ActivatedRoute,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.route.queryParams.subscribe(params => {
      this.caseDocumentId = (params['document_id'] && params['ref'] === 'audit-review')
        ? parseInt(params['document_id']) : null;
    });
    this.loadExceptions();
  }

  backToAuditReview() {
    if (!this.caseDocumentId) return;
    this.router.navigate(['/auditor/record-detail'], { queryParams: { document_id: this.caseDocumentId } });
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadExceptions() {
    this.isLoading = true;
    this.errorMessage = '';
    // Fetch the full "all" set once (limit high enough to cover this
    // app's demo/FYP-scale volume) so filter chip counts and filtering
    // can happen client-side without a round-trip per chip.
    this.http.get<any[]>(`${this.apiUrl}/auditor/exceptions?type=all&limit=200`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.exceptions = res || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load exceptions.';
        this.cdr.detectChanges();
      }
    });
  }

  setFilter(type: ExceptionType) {
    this.activeFilter = type;
  }

  get filteredExceptions() {
    let rows = this.exceptions;
    if (this.caseDocumentId) rows = rows.filter(e => e.invoice_document_id === this.caseDocumentId);
    if (this.activeFilter !== 'all') rows = rows.filter(e => e.exception_type === this.activeFilter);
    return rows;
  }

  countFor(type: ExceptionType): number {
    if (type === 'all') return this.exceptions.length;
    return this.exceptions.filter(e => e.exception_type === type).length;
  }

  investigate(exc: any) {
    this.router.navigate(['/auditor/record-detail'], {
      queryParams: { document_id: exc.invoice_document_id }
    });
  }

  severityIcon(sev: string): string {
    return 'ph-circle';
  }

  severityColor(sev: string): string {
    return sev === 'high' ? 'var(--danger)' : 'var(--warning)';
  }

  severityLabel(sev: string): string {
    if (sev === 'high') return 'HIGH';
    if (sev === 'medium') return 'MED';
    return 'LOW';
  }

  relativeTime(dateStr: string): string {
    if (!dateStr) return '-';
    const then = new Date(dateStr).getTime();
    const diffMs = Date.now() - then;
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr} hr ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay === 1) return '1 day ago';
    if (diffDay < 30) return `${diffDay} days ago`;
    return new Date(dateStr).toLocaleDateString('en-MY', { day: '2-digit', month: 'short', year: 'numeric' });
  }
}
