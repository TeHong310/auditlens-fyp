import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, RouterModule } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

// Exception type -> short label. Mirrors the literal 'type' strings
// routes/matching.py::detect_exceptions() writes into exceptions.
// exception_type (invoice_po_mismatch / invoice_gr_mismatch /
// po_gr_mismatch) - display-only, no new classification.
const EXCEPTION_TYPE_LABELS: Record<string, string> = {
  invoice_po_mismatch: 'Invoice / PO Mismatch',
  invoice_gr_mismatch: 'Invoice / GR Mismatch',
  po_gr_mismatch: 'PO / GR Mismatch',
};

interface AttentionItem {
  tier: 'critical' | 'attention';
  icon: string;
  title: string;
  subtitle: string;
  timeLabel: string | null;
  sortTime: number;
  documentId?: number;
}

interface ActivityEntry {
  icon: string;
  text: string;
  timestamp: string;
}

// Admin Dashboard — Audit Command Centre + Evidence & Risk Workspace
// hybrid. Reads GET /admin/statistics (now also carrying a small
// read-only authenticity "failed" count, added for Risk Overview only)
// plus the EXISTING, unmodified GET /admin/users, GET /admin/documents
// and GET /admin/exceptions (already used by User/Document Management
// and the Auditor's own Exceptions page respectively) - every number
// shown here is either returned directly by those endpoints or a
// plain client-side sort/filter/count over their response. Nothing
// here computes matching, authenticity, or anomaly logic of its own.
@Component({
  selector: 'app-admin-dashboard',
  standalone: true,
  imports: [CommonModule, RouterModule],
  templateUrl: './admin-dashboard.component.html',
  styleUrls: ['./admin-dashboard.component.css']
})
export class AdminDashboardComponent implements OnInit, AfterViewInit {
  @ViewChild('trendChart') trendChartRef!: ElementRef;

  user: any = {};
  isLoading: boolean = false;
  errorMessage: string = '';
  chartReady: boolean = false;

  stats: any = null;
  private users: any[] = [];
  private documents: any[] = [];
  private exceptions: any[] = [];
  private statsLoaded = false;
  private usersLoaded = false;
  private documentsLoaded = false;
  private exceptionsLoaded = false;

  // Risk Overview
  riskCritical = 0;
  riskAttention = 0;
  riskNormal = 0;
  get riskTotal(): number { return this.riskCritical + this.riskAttention + this.riskNormal; }
  get riskCriticalPct(): number { return this.riskTotal > 0 ? (this.riskCritical / this.riskTotal) * 100 : 0; }
  get riskAttentionPct(): number { return this.riskTotal > 0 ? (this.riskAttention / this.riskTotal) * 100 : 0; }
  get riskNormalPct(): number { return this.riskTotal > 0 ? (this.riskNormal / this.riskTotal) * 100 : 0; }

  // Items Requiring Attention (capped list)
  attentionItems: AttentionItem[] = [];
  attentionItemsTotal = 0;

  // Recent Activity (compact combined feed)
  activityFeed: ActivityEntry[] = [];

  // Role Distribution (supporting, plain bar list)
  roleBars: { label: string; value: number; colorVar: string; widthPct: number }[] = [];

  // Document Status Breakdown (supporting, plain segmented strip)
  statusSegments: { label: string; value: number; colorVar: string }[] = [];

  private trendChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(private http: HttpClient, private router: Router, private cdr: ChangeDetectorRef) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      this.user = JSON.parse(localStorage.getItem('user') || '{}');
    }
    this.loadStatistics();
    this.loadUsers();
    this.loadDocuments();
    this.loadExceptions();
  }

  ngAfterViewInit() {
    if (this.chartReady) {
      this.renderTrendChart();
    }
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadStatistics() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/admin/statistics`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.stats = res;
        this.statsLoaded = true;
        this.isLoading = false;
        this.chartReady = true;
        this.cdr.detectChanges();
        setTimeout(() => this.renderTrendChart(), 150);
        this.tryComputeDerived();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load system statistics.';
        this.cdr.detectChanges();
      }
    });
  }

  loadUsers() {
    this.http.get<any>(`${this.apiUrl}/admin/users`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.users = res.users || [];
        this.usersLoaded = true;
        this.tryComputeDerived();
      },
      error: () => { this.usersLoaded = true; }
    });
  }

  loadDocuments() {
    this.http.get<any>(`${this.apiUrl}/admin/documents`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.documents = res.documents || [];
        this.documentsLoaded = true;
        this.tryComputeDerived();
      },
      error: () => { this.documentsLoaded = true; }
    });
  }

  loadExceptions() {
    this.http.get<any>(`${this.apiUrl}/admin/exceptions`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.exceptions = res.exceptions || [];
        this.exceptionsLoaded = true;
        this.tryComputeDerived();
      },
      error: () => { this.exceptionsLoaded = true; }
    });
  }

  private tryComputeDerived() {
    if (!this.statsLoaded || !this.usersLoaded || !this.documentsLoaded || !this.exceptionsLoaded) return;
    this.computeRiskBuckets();
    this.computeAttentionItems();
    this.computeActivityFeed();
    this.computeRoleBars();
    this.computeStatusSegments();
    this.cdr.detectChanges();
  }

  // ── Risk Overview ──────────────────────────────────────────
  // Critical = high-severity unresolved exceptions + hard matching
  // mismatches (stats.matching.mismatch, the existing record_matches
  // engine's own verdict) + failed authenticity checks (stats.
  // authenticity.failed, the existing authenticity engine's own
  // risk_level='HIGH' verdict). Attention = returned/under_review/
  // resubmitted documents. Normal = approved documents. Every count
  // comes directly from already-fetched API data - no new scoring.
  private computeRiskBuckets() {
    const highUnresolvedExceptions = this.exceptions.filter(e => e.severity === 'high' && !e.is_resolved).length;
    const mismatches = this.stats?.matching?.mismatch || 0;
    const authFailed = this.stats?.authenticity?.failed || 0;
    this.riskCritical = highUnresolvedExceptions + mismatches + authFailed;

    const d = this.stats?.documents || {};
    this.riskAttention = (d.returned || 0) + (d.under_review || 0) + (d.resubmitted || 0);

    this.riskNormal = d.approved || 0;
  }

  // ── Items Requiring Attention ───────────────────────────────
  private computeAttentionItems() {
    const items: AttentionItem[] = [];

    for (const e of this.exceptions.filter(x => x.severity === 'high' && !x.is_resolved)) {
      items.push({
        tier: 'critical',
        icon: 'ph-warning-octagon',
        title: EXCEPTION_TYPE_LABELS[e.exception_type] || 'Exception Flagged',
        subtitle: `${e.file_name || 'Unknown document'}${e.description ? ' — ' + e.description : ''}`,
        timeLabel: this.relativeTime(e.created_at),
        sortTime: this.toTime(e.created_at),
        documentId: e.document_id,
      });
    }

    for (const d of this.documents.filter(x => x.status === 'returned')) {
      items.push({
        tier: 'attention',
        icon: 'ph-arrow-u-up-left',
        title: 'Returned to Finance',
        subtitle: `${d.document_number} · ${d.vendor_name || 'Unknown vendor'}`,
        timeLabel: this.relativeTime(d.updated_at || d.uploaded_at),
        sortTime: this.toTime(d.updated_at || d.uploaded_at),
        documentId: d.document_id,
      });
    }

    for (const d of this.documents.filter(x => x.status === 'resubmitted')) {
      items.push({
        tier: 'attention',
        icon: 'ph-arrow-clockwise',
        title: 'Awaiting Auditor Re-Review',
        subtitle: `${d.document_number} · ${d.vendor_name || 'Unknown vendor'}`,
        timeLabel: this.relativeTime(d.updated_at || d.uploaded_at),
        sortTime: this.toTime(d.updated_at || d.uploaded_at),
        documentId: d.document_id,
      });
    }

    for (const u of this.users.filter(x => !x.is_active)) {
      items.push({
        tier: 'attention',
        icon: 'ph-user-minus',
        title: 'Account Disabled',
        subtitle: u.email,
        // created_at is the account's registration date, not when it
        // was disabled - shown only as a sort tie-breaker, never
        // rendered as a "disabled X ago" claim, since that data isn't
        // tracked anywhere.
        timeLabel: null,
        sortTime: this.toTime(u.created_at),
      });
    }

    items.sort((a, b) => {
      if (a.tier !== b.tier) return a.tier === 'critical' ? -1 : 1;
      return b.sortTime - a.sortTime;
    });

    this.attentionItemsTotal = items.length;
    this.attentionItems = items.slice(0, 8);
  }

  openAttentionItem(item: AttentionItem) {
    if (item.documentId) {
      this.router.navigate(['/admin/record-detail'], { queryParams: { document_id: item.documentId } });
    } else {
      this.router.navigate(['/admin/users']);
    }
  }

  // ── Recent Activity (compact combined feed) ─────────────────
  private computeActivityFeed() {
    const items: ActivityEntry[] = [];

    for (const u of this.users) {
      items.push({
        icon: 'ph-user-plus',
        text: `New user registered — ${u.full_name} (${this.roleLabel(u.role)})`,
        timestamp: u.created_at,
      });
    }
    for (const d of this.documents) {
      items.push({
        icon: 'ph-file-plus',
        text: `Document uploaded — ${d.document_number}${d.vendor_name ? ' · ' + d.vendor_name : ''}`,
        timestamp: d.uploaded_at,
      });
    }

    items.sort((a, b) => this.toTime(b.timestamp) - this.toTime(a.timestamp));
    this.activityFeed = items.slice(0, 8);
  }

  // ── Supporting: Role Distribution (plain bar list) ──────────
  private computeRoleBars() {
    const u = this.stats?.users || {};
    const max = Math.max(u.total_finance || 0, u.total_auditors || 0, u.total_admins || 0, 1);
    const rows = [
      { label: 'Finance Executive', value: u.total_finance || 0, colorVar: '--accent' },
      { label: 'Auditor', value: u.total_auditors || 0, colorVar: '--warning' },
      { label: 'Admin', value: u.total_admins || 0, colorVar: '--accent-soft' },
    ];
    this.roleBars = rows.map(r => ({ ...r, widthPct: (r.value / max) * 100 }));
  }

  // ── Supporting: Document Status Breakdown (plain segmented strip) ──
  private computeStatusSegments() {
    const d = this.stats?.documents || {};
    this.statusSegments = [
      { label: 'Under Review', value: d.under_review || 0, colorVar: '--warning' },
      { label: 'Approved', value: d.approved || 0, colorVar: '--success' },
      { label: 'Returned', value: d.returned || 0, colorVar: '--danger' },
      { label: 'Resubmitted', value: d.resubmitted || 0, colorVar: '--accent-soft' },
    ];
  }

  get statusSegmentsTotal(): number {
    return this.statusSegments.reduce((sum, s) => sum + s.value, 0) || 1;
  }

  get allDataLoaded(): boolean {
    return this.statsLoaded && this.usersLoaded && this.documentsLoaded && this.exceptionsLoaded;
  }

  // ── Supporting: Monthly Upload Trend (small sparkline chart) ───
  renderTrendChart() {
    if (!this.trendChartRef || !this.stats) return;
    if (this.trendChartInstance) this.trendChartInstance.destroy();

    const monthly: { month: string; count: number }[] = this.stats.monthly_uploads || [];
    const labels = monthly.map(m => this.formatMonth(m.month));
    const data = monthly.map(m => m.count);

    const ctx = this.trendChartRef.nativeElement.getContext('2d');
    this.trendChartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data,
          borderColor: '#6C4FFF',
          backgroundColor: 'rgba(108, 79, 255, 0.12)',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.35,
          fill: true,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: true } },
        scales: {
          y: { display: false, beginAtZero: true },
          x: { display: false }
        }
      }
    });
  }

  private formatMonth(yyyyMm: string): string {
    if (!yyyyMm) return '';
    const [year, month] = yyyyMm.split('-');
    const date = new Date(Number(year), Number(month) - 1, 1);
    return date.toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
  }

  private toTime(dateStr: string | null | undefined): number {
    return dateStr ? new Date(dateStr).getTime() : 0;
  }

  relativeTime(dateStr: string | null | undefined): string {
    if (!dateStr) return '-';
    const diffMs = Date.now() - new Date(dateStr).getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.floor(diffHr / 24);
    return `${diffDay}d ago`;
  }

  roleLabel(role: string): string {
    if (role === 'finance_executive') return 'Finance Executive';
    if (role === 'auditor') return 'Auditor';
    if (role === 'admin') return 'Admin';
    return role;
  }
}
