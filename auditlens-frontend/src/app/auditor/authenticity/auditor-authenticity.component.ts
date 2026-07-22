import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';
import { getAuthenticityEvidenceRows, EvidenceRow } from '../shared/authenticity-evidence.util';

type Filter = 'all' | 'passed' | 'warning';
type DocTypeFilter = 'all' | 'invoice' | 'po' | 'gr';

@Component({
  selector: 'app-auditor-authenticity',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-authenticity.component.html',
  styleUrls: ['./auditor-authenticity.component.css']
})
export class AuditorAuthenticityComponent implements OnInit {

  checks: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';
  activeFilter: Filter = 'all';
  activeDocTypeFilter: DocTypeFilter = 'all';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadChecks();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // Enterprise V3 Phase 7 (FIX 2): a single call now returns every
  // document's transaction_context alongside its authenticity fields
  // (routes/authenticity.py's _with_authentication_score enriches every
  // row), so the page renders once — no separate N+1 "load transaction
  // groups, then load documents" sequence, and no layout jump between
  // the two.
  loadChecks() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any[]>(`${this.apiUrl}/authenticity`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.checks = res || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load authenticity checks.';
        this.cdr.detectChanges();
      }
    });
  }

  setFilter(f: Filter) {
    this.activeFilter = f;
  }

  setDocTypeFilter(f: DocTypeFilter) {
    this.activeDocTypeFilter = f;
  }

  // Status filter and document-type filter combine with AND — e.g.
  // Invoice + Passed shows only passed invoices. Client-side only, over
  // the already-loaded list (no pagination on this endpoint currently).
  get filteredChecks() {
    return this.checks.filter(c =>
      (this.activeFilter === 'all' || c.authenticity_status === this.activeFilter) &&
      (this.activeDocTypeFilter === 'all' || c.document_type === this.activeDocTypeFilter)
    );
  }

  // Each stat/chip reflects only its own dimension (all documents
  // matching that status/type), independent of whatever else is
  // currently selected in the other filter row.
  get passedCount(): number {
    return this.checks.filter(c => c.authenticity_status === 'passed').length;
  }

  get warningCount(): number {
    return this.checks.filter(c => c.authenticity_status === 'warning').length;
  }

  get invoiceCount(): number {
    return this.checks.filter(c => c.document_type === 'invoice').length;
  }

  get poCount(): number {
    return this.checks.filter(c => c.document_type === 'po').length;
  }

  get grCount(): number {
    return this.checks.filter(c => c.document_type === 'gr').length;
  }

  viewDocument(check: any) {
    this.router.navigate(['/auditor/authenticity', check.document_id], {
      queryParams: { document_type: check.document_type }
    });
  }

  // Enterprise V3 Phase 7 (FIX 3): shared evidence interpretation, same
  // function the Authenticity Detail page uses — a document's "Detected
  // Signals" badges can no longer disagree with what its own detail page
  // shows for the same check.
  evidenceRows(check: any): EvidenceRow[] {
    return getAuthenticityEvidenceRows(check, check.document_type);
  }

  rowIconClass(status: string): string {
    return { yes: 'badge-yes', no: 'badge-no', warn: 'badge-warn', na: 'badge-na' }[status] || 'badge-na';
  }

  rowIcon(status: string): string {
    return { yes: 'ph-check', no: 'ph-x', warn: 'ph-warning', na: 'ph-minus' }[status] || 'ph-minus';
  }

  docTypeLabel(type: string): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'PO';
    if (type === 'gr') return 'GR';
    return type || 'Unknown';
  }

  uploadSourceIcon(source: string): string {
    if (source === 'phone_photo') return 'ph-device-mobile-camera';
    if (source === 'scanned') return 'ph-printer';
    if (source === 'digital_native') return 'ph-desktop';
    if (source === 'webcam') return 'ph-webcam';
    return 'ph-question';
  }

  uploadSourceLabel(source: string): string {
    if (source === 'phone_photo') return 'Phone Photo';
    if (source === 'scanned') return 'Scanned';
    if (source === 'digital_native') return 'Digital Native';
    if (source === 'webcam') return 'Webcam';
    return 'Unknown';
  }

  warningReason(check: any): string {
    const type = (check.document_type || '').toLowerCase();
    const missing: string[] = [];
    if (!check.has_company_name) missing.push('company name');
    if (type === 'invoice' && !check.has_company_chop && !check.has_signature) {
      missing.push('chop/signature');
    }
    if (missing.length === 0) return '';
    return `Missing ${missing.join(' and ')} (required for ${this.docTypeLabel(check.document_type)})`;
  }

  relativeTime(dateStr: string): string {
    if (!dateStr) return '-';
    const diffMs = Date.now() - new Date(dateStr).getTime();
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
