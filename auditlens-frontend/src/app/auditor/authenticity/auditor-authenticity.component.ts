import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';
import { getAuthenticityEvidenceRows, EvidenceRow } from '../shared/authenticity-evidence.util';

Chart.register(...registerables);

type Filter = 'all' | 'PASS' | 'REVIEW' | 'FAIL';
type DocTypeFilter = 'all' | 'invoice' | 'po' | 'gr';

// Same palette as Auditor Home's dashboard (auditor-dashboard.component.
// ts) — kept as its own copy per this codebase's per-component
// convention, used only for chart decoration.
const CHART_PALETTE = {
  violet: '#8B5CF6', blue: '#3B82F6', cyan: '#22D3EE', teal: '#2DD4BF',
  green: '#34D399', amber: '#FBBF24', orange: '#FB923C', coral: '#FB7185',
  red: '#F43F5E', pink: '#F472B6',
};

@Component({
  selector: 'app-auditor-authenticity',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-authenticity.component.html',
  styleUrls: ['./auditor-authenticity.component.css']
})
export class AuditorAuthenticityComponent implements OnInit, AfterViewInit {
  @ViewChild('evidenceChart') evidenceChartRef!: ElementRef;

  checks: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';
  activeFilter: Filter = 'all';
  activeDocTypeFilter: DocTypeFilter = 'all';

  private viewReady = false;
  private evidenceChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadChecks();
  }

  ngAfterViewInit() {
    this.viewReady = true;
    this.renderEvidenceChart();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // Enterprise V3 Phase 7 (FIX 2): a single call now returns every
  // document's transaction_context alongside its authenticity fields
  // (routes/authenticity.py's _with_authentication_score enriches every
  // row — including the document-type-aware authentication_status/
  // authentication_summary/risk_level this redesign now surfaces), so
  // the page renders once — no separate N+1 "load transaction groups,
  // then load documents" sequence, and no layout jump between the two.
  loadChecks() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any[]>(`${this.apiUrl}/authenticity`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.checks = res || [];
        this.isLoading = false;
        this.cdr.detectChanges();
        this.renderEvidenceChart();
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
  // Filters on authentication_status (the richer PASS/REVIEW/FAIL
  // status already computed server-side by helpers/auth_rules.py, part
  // of every /authenticity response) rather than the older binary
  // authenticity_status column, so "Risk Detected" is a real, distinct
  // filterable category instead of being folded into "Review Required".
  get filteredChecks() {
    return this.checks.filter(c =>
      (this.activeFilter === 'all' || c.authentication_status === this.activeFilter) &&
      (this.activeDocTypeFilter === 'all' || c.document_type === this.activeDocTypeFilter)
    );
  }

  get passedCount(): number {
    return this.checks.filter(c => c.authentication_status === 'PASS').length;
  }

  get reviewCount(): number {
    return this.checks.filter(c => c.authentication_status === 'REVIEW').length;
  }

  get failCount(): number {
    return this.checks.filter(c => c.authentication_status === 'FAIL').length;
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

  pct(n: number): string {
    return this.checks.length > 0 ? ((n / this.checks.length) * 100).toFixed(1) : '0';
  }

  // Mini radial progress ring — pure CSS conic-gradient, same technique
  // as Auditor Home's Status Breakdown / Finance Home's Document Status
  // Overview.
  ringGradient(value: number, total: number, color: string): string {
    const percent = total > 0 ? (value / total) * 100 : 0;
    return `conic-gradient(${color} 0% ${percent}%, var(--bg-hover) ${percent}% 100%)`;
  }

  // Missing Evidence Analysis — counts across ALL loaded checks (not
  // just the currently-filtered subset, matching how the KPI cards
  // above also reflect the whole dataset) of documents missing each
  // raw detected-signal boolean already on every authenticity_checks
  // row. No new computation — has_signature/has_company_logo/
  // has_company_chop/has_company_name are the same fields the old
  // "Detected Signals" badges already read.
  private missingEvidenceCounts(): { label: string; value: number }[] {
    return [
      { label: 'Missing Signature', value: this.checks.filter(c => !c.has_signature).length },
      { label: 'Missing Logo', value: this.checks.filter(c => !c.has_company_logo).length },
      { label: 'Missing Company Chop', value: this.checks.filter(c => !c.has_company_chop).length },
      { label: 'Missing Company Name', value: this.checks.filter(c => !c.has_company_name).length },
    ];
  }

  renderEvidenceChart() {
    if (!this.viewReady || !this.evidenceChartRef || this.checks.length === 0) return;
    if (this.evidenceChartInstance) this.evidenceChartInstance.destroy();

    const cats = this.missingEvidenceCounts();
    const categoryColors = [CHART_PALETTE.coral, CHART_PALETTE.amber, CHART_PALETTE.violet, CHART_PALETTE.blue];
    const ctx = this.evidenceChartRef.nativeElement.getContext('2d');
    this.evidenceChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: cats.map(c => c.label),
        datasets: [{
          data: cats.map(c => c.value),
          backgroundColor: cats.map((_, i) => categoryColors[i % categoryColors.length]),
          borderRadius: 4, borderSkipped: false,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false, beginAtZero: true },
          y: { ticks: { font: { size: 10 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  viewDocument(check: any) {
    this.router.navigate(['/auditor/authenticity', check.document_id], {
      queryParams: { document_type: check.document_type }
    });
  }

  // "View Document" — opens the original uploaded file directly, same
  // authenticated-blob-fetch pattern already used by auditor-record-
  // detail.component.ts's openTransactionDocument()/fileUrlFor(), reusing
  // the SAME existing file-serving endpoints (GET /documents/<id>/file,
  // GET /documents/po/<po_id>/file, GET /documents/gr/<gr_id>/file) —
  // no new backend route.
  viewRawDocument(check: any, event: Event) {
    event.stopPropagation();
    let path: string;
    if (check.document_type === 'po' && check.po_id) path = `po/${check.po_id}/file`;
    else if (check.document_type === 'gr' && check.gr_id) path = `gr/${check.gr_id}/file`;
    else path = `${check.document_id}/file`;

    const token = localStorage.getItem('access_token');
    fetch(`${this.apiUrl}/documents/${path}`, { headers: { 'Authorization': `Bearer ${token}` } })
      .then(res => { if (!res.ok) throw new Error('Failed'); return res.blob(); })
      .then(blob => window.open(URL.createObjectURL(blob), '_blank'))
      .catch(() => { this.errorMessage = 'Failed to open document file.'; this.cdr.detectChanges(); });
  }

  // Enterprise V3 Phase 7 (FIX 3): shared evidence interpretation, same
  // function the Authenticity Detail page uses — a document's evidence
  // checklist can no longer disagree with what its own detail page
  // shows for the same check. Document-type-aware (an invoice's
  // Signature row, a PO/GR's letterhead+optional-logo rows, etc.) —
  // deliberately NOT collapsed into one fixed 4-item list, since doing
  // so would reintroduce the exact false-negative bug (e.g. a PO's
  // always-absent-by-design signature reading as a failure) this
  // shared util was built to fix.
  evidenceRows(check: any): EvidenceRow[] {
    return getAuthenticityEvidenceRows(check, check.document_type);
  }

  rowIcon(status: string): string {
    return { yes: 'ph-check-circle', no: 'ph-x-circle', warn: 'ph-warning-circle', na: 'ph-minus-circle' }[status] || 'ph-minus-circle';
  }

  // 3-way authentication_status (PASS/REVIEW/FAIL) badge — computed
  // server-side by helpers/auth_rules.py, already part of every
  // /authenticity response.
  statusBadgeClass(status: string): string {
    if (status === 'PASS') return 'status-pass';
    if (status === 'FAIL') return 'status-fail';
    return 'status-review'; // REVIEW, or a defensive fallback
  }

  statusLabel(status: string): string {
    if (status === 'PASS') return 'Passed';
    if (status === 'FAIL') return 'Failed';
    if (status === 'REVIEW') return 'Review Required';
    return status || 'Unknown';
  }

  riskBadgeClass(level: string): string {
    const l = (level || '').toUpperCase();
    if (l === 'HIGH') return 'risk-high';
    if (l === 'MEDIUM') return 'risk-medium';
    return 'risk-low';
  }

  riskLabel(level: string): string {
    const l = (level || 'LOW').toUpperCase();
    return l.charAt(0) + l.slice(1).toLowerCase();
  }

  docTypeLabel(type: string): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'PO';
    if (type === 'gr') return 'GR';
    return type || 'Unknown';
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
