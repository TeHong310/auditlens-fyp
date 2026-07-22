import { Component, OnInit, AfterViewInit, ViewChild, ElementRef, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

type Period = 'today' | 'week' | 'month' | 'all';
type ActionFilter = 'all' | 'approved' | 'sent_back' | 'need_review';

@Component({
  selector: 'app-auditor-report',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-report.component.html',
  styleUrls: ['./auditor-report.component.css']
})
export class AuditorReportComponent implements OnInit, AfterViewInit {
  @ViewChild('timelineChart') timelineChartRef!: ElementRef;

  periods: { key: Period; label: string }[] = [
    { key: 'today', label: 'Today' },
    { key: 'week', label: 'This Week' },
    { key: 'month', label: 'This Month' },
    { key: 'all', label: 'All Time' },
  ];
  activePeriod: Period = 'month';

  stats: any = { approved: 0, sent_back: 0, pending: 0, exceptions: 0, match_pass: 0, match_review: 0 };
  timeline: any[] = [];
  isLoadingSummary: boolean = false;
  summaryError: string = '';

  // ── Audit Quality Overview ──────────────────────────────
  // Three-way match PASS/REVIEW comes from the existing summary
  // response (stats.match_pass/match_review) — no extra call.
  // Authenticity and Anomaly each reuse an existing, already-built
  // endpoint (GET /authenticity, GET /anomalies/stats) and are counted
  // here rather than adding new backend endpoints. null = not yet
  // loaded / unavailable -> template shows a graceful empty state,
  // never fabricated numbers.
  authenticityQuality: { passed: number; warning: number } | null = null;
  anomalyQuality: { high: number; medium: number } | null = null;
  isLoadingQuality: boolean = false;

  // Audit trail
  entries: any[] = [];
  totalEntries: number = 0;
  activeAction: ActionFilter = 'all';
  startDate: string = '';
  endDate: string = '';
  offset: number = 0;
  pageSize: number = 50;
  isLoadingTrail: boolean = false;
  trailError: string = '';

  private chartInstance: any = null;
  private chartReady: boolean = false;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadSummary();
    this.loadAuditTrail(true);
    this.loadQualityOverview();
  }

  ngAfterViewInit() {
    if (this.chartReady) this.renderChart();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── Summary + chart ──────────────────────────────────────

  setPeriod(p: Period) {
    this.activePeriod = p;
    this.loadSummary();
  }

  loadSummary() {
    this.isLoadingSummary = true;
    this.summaryError = '';
    this.http.get<any>(`${this.apiUrl}/auditor/report/summary?period=${this.activePeriod}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.stats = res.stats;
        this.timeline = res.timeline || [];
        this.isLoadingSummary = false;
        this.chartReady = true;
        this.cdr.detectChanges();
        setTimeout(() => this.renderChart(), 100);
      },
      error: (err) => {
        this.isLoadingSummary = false;
        this.summaryError = err.error?.error || 'Failed to load report summary.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Audit Quality Overview ──────────────────────────────
  // Authenticity + Anomaly counts reuse existing endpoints already used
  // elsewhere in the app (auditor-authenticity.component.ts /
  // auditor-anomalies.component.ts) — read-only, no new backend route.
  // A fetch failure leaves the corresponding field null so the template
  // renders a graceful empty state instead of a fabricated number.

  loadQualityOverview() {
    this.isLoadingQuality = true;
    const headers = this.getHeaders();

    this.http.get<any[]>(`${this.apiUrl}/authenticity`, { headers }).subscribe({
      next: (res) => {
        const rows = res || [];
        this.authenticityQuality = {
          passed:  rows.filter(r => r.authenticity_status === 'passed').length,
          warning: rows.filter(r => r.authenticity_status === 'warning').length,
        };
        this.cdr.detectChanges();
      },
      error: () => {
        this.authenticityQuality = null;
        this.cdr.detectChanges();
      }
    });

    this.http.get<any>(`${this.apiUrl}/anomalies/stats`, { headers }).subscribe({
      next: (res) => {
        this.anomalyQuality = {
          high:   res?.by_severity?.high ?? 0,
          medium: res?.by_severity?.medium ?? 0,
        };
        this.isLoadingQuality = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.anomalyQuality = null;
        this.isLoadingQuality = false;
        this.cdr.detectChanges();
      }
    });
  }

  get hasMatchQuality(): boolean {
    return !this.isLoadingSummary && !this.summaryError;
  }

  renderChart() {
    if (!this.timelineChartRef) return;
    if (this.chartInstance) this.chartInstance.destroy();

    const labels = this.timeline.map(t => this.formatChartDate(t.date));
    const ctx = this.timelineChartRef.nativeElement.getContext('2d');
    this.chartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Approved',
            data: this.timeline.map(t => t.approved),
            borderColor: '#10B981',
            backgroundColor: 'rgba(16,185,129,0.08)',
            tension: 0.3,
            fill: true,
            pointRadius: 2,
          },
          {
            label: 'Sent Back',
            data: this.timeline.map(t => t.sent_back),
            borderColor: '#EF4444',
            backgroundColor: 'rgba(239,68,68,0.08)',
            tension: 0.3,
            fill: true,
            pointRadius: 2,
          },
          {
            label: 'Pending',
            data: this.timeline.map(t => t.pending),
            borderColor: '#F59E0B',
            backgroundColor: 'rgba(245,158,11,0.08)',
            tension: 0.3,
            fill: true,
            pointRadius: 2,
          },
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index' as const, intersect: false },
        plugins: {
          legend: {
            position: 'bottom' as const,
            labels: { boxWidth: 10, padding: 14, font: { size: 11 } }
          }
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 10, font: { size: 10 } } },
          y: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 }, grid: { color: '#F3F4F6' } }
        }
      }
    });
  }

  formatChartDate(dateStr: string): string {
    return new Date(dateStr).toLocaleDateString('en-MY', { day: '2-digit', month: 'short' });
  }

  // ── Audit trail ──────────────────────────────────────────

  setActionFilter(a: ActionFilter) {
    this.activeAction = a;
    this.loadAuditTrail(true);
  }

  applyDateRange() {
    this.loadAuditTrail(true);
  }

  loadAuditTrail(reset: boolean) {
    if (reset) {
      this.offset = 0;
      this.entries = [];
    }
    this.isLoadingTrail = true;
    this.trailError = '';

    let url = `${this.apiUrl}/auditor/report/audit-trail?action=${this.activeAction}&limit=${this.pageSize}&offset=${this.offset}`;
    if (this.startDate) url += `&start_date=${this.startDate}`;
    if (this.endDate) url += `&end_date=${this.endDate}`;

    this.http.get<any>(url, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.entries = reset ? (res.entries || []) : [...this.entries, ...(res.entries || [])];
        this.totalEntries = res.total || 0;
        this.isLoadingTrail = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoadingTrail = false;
        this.trailError = err.error?.error || 'Failed to load audit trail.';
        this.cdr.detectChanges();
      }
    });
  }

  loadMore() {
    this.offset += this.pageSize;
    this.loadAuditTrail(false);
  }

  get hasMore(): boolean {
    return this.entries.length < this.totalEntries;
  }

  // Recent Review Activity table (Feature 5) — reuses the SAME entries
  // array already loaded for the Audit Trail below it, just the most
  // recent handful in a compact table. No second fetch.
  get recentActivity(): any[] {
    return this.entries.slice(0, 8);
  }

  // Status shown alongside each Audit Trail entry / Recent Activity row
  // — a truthful restatement of what the recorded action itself already
  // means, not a live lookup of the document's current status (which
  // can have moved on since this historical entry, e.g. sent back then
  // later resubmitted and approved) and requires no schema/query change.
  statusForAction(action: string): string {
    if (action === 'approved') return 'Approved';
    if (action === 'sent_back') return 'Awaiting Finance correction';
    if (action === 'need_review') return 'Under auditor review';
    return '-';
  }

  exportCsv() {
    let url = `${this.apiUrl}/auditor/report/audit-trail/export.csv?action=${this.activeAction}&limit=1000&offset=0`;
    if (this.startDate) url += `&start_date=${this.startDate}`;
    if (this.endDate) url += `&end_date=${this.endDate}`;

    this.http.get(url, { headers: this.getHeaders(), responseType: 'blob' }).subscribe({
      next: (blob) => {
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = `audit_trail_${new Date().toISOString().slice(0, 10)}.csv`;
        a.click();
        URL.revokeObjectURL(objUrl);
      },
      error: () => {
        this.trailError = 'Failed to export CSV.';
        this.cdr.detectChanges();
      }
    });
  }

  viewInvoice(entry: any) {
    if (!entry.invoice_document_id) return;
    this.router.navigate(['/auditor/record-detail'], {
      queryParams: { document_id: entry.invoice_document_id }
    });
  }

  actionPillClass(action: string): string {
    if (action === 'approved') return 'pill-approved';
    if (action === 'sent_back') return 'pill-sent-back';
    return 'pill-need-review';
  }

  actionLabel(action: string): string {
    if (action === 'approved') return 'Approved';
    if (action === 'sent_back') return 'Sent Back';
    return 'Need Review';
  }

  actionIcon(action: string): string {
    if (action === 'approved') return 'ph-check-circle';
    if (action === 'sent_back') return 'ph-arrow-u-up-left';
    return 'ph-warning';
  }

  formatTimestamp(ts: string): string {
    if (!ts) return '-';
    const d = new Date(ts);
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    if (isToday) return this.relativeTime(ts);
    return d.toLocaleDateString('en-MY', { day: '2-digit', month: 'short' }) + ' ' +
      d.toLocaleTimeString('en-MY', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  relativeTime(ts: string): string {
    const diffMs = Date.now() - new Date(ts).getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffHr = Math.floor(diffMin / 60);
    return `${diffHr} hr ago`;
  }
}
