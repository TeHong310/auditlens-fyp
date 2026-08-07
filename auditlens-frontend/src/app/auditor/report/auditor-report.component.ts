import { Component, OnInit, AfterViewInit, ViewChild, ElementRef, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';
import { formatMalaysiaDateTime } from '../../shared/datetime.util';

Chart.register(...registerables);

type Period = 'today' | 'week' | 'month' | 'all';
type ActionFilter = 'all' | 'approved' | 'sent_back' | 'need_review';

// Shared trend-line palette (Report dashboard redesign) — reused as-is
// for the new Review Outcome Distribution doughnut so every chart on
// this page describes Approved/Sent Back/Need Review with the same
// three colors.
const ACTION_COLORS = {
  approved:    '#4FD1B5',
  sent_back:   '#F45B69',
  need_review: '#F5B83D',
};

// Ad-hoc Chart.js v4 plugin (no new npm dependency) — draws each bar's
// numeric value just past its end. Passed per-chart via the `plugins`
// array, not globally registered, so it only affects Audit Findings
// Breakdown (the one chart that asked for value labels).
const valueLabelPlugin = {
  id: 'valueLabelPlugin',
  afterDatasetsDraw(chart: any) {
    const { ctx } = chart;
    chart.data.datasets.forEach((dataset: any, datasetIndex: number) => {
      const meta = chart.getDatasetMeta(datasetIndex);
      meta.data.forEach((bar: any, index: number) => {
        const value = dataset.data[index];
        if (value === null || value === undefined) return;
        ctx.save();
        ctx.fillStyle = '#E6E7EE';
        ctx.font = '600 11px Inter, sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(value), bar.x + 6, bar.y);
        ctx.restore();
      });
    });
  }
};

@Component({
  selector: 'app-auditor-report',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-report.component.html',
  styleUrls: ['./auditor-report.component.css']
})
export class AuditorReportComponent implements OnInit, AfterViewInit {
  @ViewChild('timelineChart') timelineChartRef!: ElementRef;
  @ViewChild('outcomeChart') outcomeChartRef!: ElementRef;
  @ViewChild('findingsChart') findingsChartRef!: ElementRef;
  @ViewChild('vendorsChart') vendorsChartRef!: ElementRef;

  periods: { key: Period; label: string }[] = [
    { key: 'today', label: 'Today' },
    { key: 'week', label: 'This Week' },
    { key: 'month', label: 'This Month' },
    { key: 'all', label: 'All Time' },
  ];
  activePeriod: Period = 'month';

  stats: any = {
    approved: 0, sent_back: 0, need_review: 0, pending: 0, exceptions: 0,
    match_pass: 0, match_review: 0, avg_review_time_hours: null,
  };
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
  //
  // authenticityQuality reads authentication_status (PASS/REVIEW/FAIL —
  // the same 3-tier field the Authenticity page's own filter chips use),
  // not the older binary authenticity_status column, so "Failed" is a
  // real, distinct count rather than folded into "Warning".
  authenticityQuality: { passed: number; warning: number; failed: number } | null = null;
  anomalyQuality: { high: number; medium: number; low: number } | null = null;
  // Anomaly type breakdown (amount/round/weekend/duplicate) — same
  // /anomalies/stats response as anomalyQuality above, just also
  // capturing by_type for the Audit Findings Breakdown chart. No second
  // call.
  anomalyByType: { amount: number; round: number; weekend: number; duplicate: number } | null = null;
  isLoadingQuality: boolean = false;

  // Top Vendors by Review Activity — a dedicated fetch of the SAME
  // /auditor/report/audit-trail endpoint the table below already uses,
  // just with a larger limit (identical to the existing exportCsv()'s
  // own limit=1000 call) so the vendor aggregation isn't skewed by
  // whatever page the Audit Trail table happens to have loaded. Always
  // all-time/all-actions, matching how Audit Quality Overview is also a
  // period-independent current snapshot rather than following the
  // period selector above.
  vendorActivityEntries: any[] = [];
  isLoadingVendorActivity: boolean = false;

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

  // ── Pagination — frontend-only, over the already-loaded entries
  // array shared by both tables below (no new backend call; distinct
  // from the existing `pageSize` above, which is the Load More fetch
  // batch size, not the on-screen rows-per-page). Each table keeps its
  // own page size so changing one never affects the other.
  reviewRowsPerPage = 10;
  auditTrailRowsPerPage = 5;
  currentReviewPage = 1;
  currentAuditTrailPage = 1;

  // Recent Review Activity — collapsed to a 5-row preview by default;
  // "View All" reveals the existing paginated (10/page) view below.
  reviewActivityExpanded = false;

  private chartInstance: any = null;
  private outcomeChartInstance: any = null;
  private findingsChartInstance: any = null;
  private vendorsChartInstance: any = null;
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
    this.loadVendorActivity();
  }

  ngAfterViewInit() {
    if (this.chartReady) this.renderChart();
    this.maybeRenderFindingsChart();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── Summary + charts ──────────────────────────────────────

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
        setTimeout(() => {
          this.renderChart();
          this.renderOutcomeChart();
          this.maybeRenderFindingsChart();
        }, 100);
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
          passed:  rows.filter(r => r.authentication_status === 'PASS').length,
          warning: rows.filter(r => r.authentication_status === 'REVIEW').length,
          failed:  rows.filter(r => r.authentication_status === 'FAIL').length,
        };
        this.cdr.detectChanges();
        setTimeout(() => this.maybeRenderFindingsChart(), 100);
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
          low:    res?.by_severity?.low ?? 0,
        };
        this.anomalyByType = {
          amount:    res?.by_type?.amount ?? 0,
          round:     res?.by_type?.round ?? 0,
          weekend:   res?.by_type?.weekend ?? 0,
          duplicate: res?.by_type?.duplicate ?? 0,
        };
        this.isLoadingQuality = false;
        this.cdr.detectChanges();
        setTimeout(() => this.maybeRenderFindingsChart(), 100);
      },
      error: () => {
        this.anomalyQuality = null;
        this.anomalyByType = null;
        this.isLoadingQuality = false;
        this.cdr.detectChanges();
      }
    });
  }

  get hasMatchQuality(): boolean {
    return !this.isLoadingSummary && !this.summaryError;
  }

  // ── KPI cards ──────────────────────────────────────────────
  // All four reuse fields already loaded above (stats.* / exceptions) —
  // no new backend call beyond avg_review_time_hours, which the summary
  // endpoint now also returns.

  get totalReviewedDocuments(): number {
    return (this.stats.approved || 0) + (this.stats.sent_back || 0);
  }

  get approvalRateLabel(): string {
    const total = this.totalReviewedDocuments;
    if (total === 0) return '—';
    return ((this.stats.approved / total) * 100).toFixed(1) + '%';
  }

  get avgReviewTimeLabel(): string {
    const hours = this.stats.avg_review_time_hours;
    if (hours === null || hours === undefined) return '—';
    if (hours < 24) return `${hours}h`;
    return `${(hours / 24).toFixed(1)}d`;
  }

  get riskFindingsCount(): number {
    return this.stats.exceptions || 0;
  }

  // ── Audit Quality Overview donuts — pure CSS conic-gradient, same
  // technique as the Authenticity page's own ringGradient(), generalized
  // to more than one non-background segment. green = clean/passing tier,
  // amber = caution tier, red = the worst tier, kept consistent with the
  // color each of these already used before this redesign (match REVIEW
  // and anomaly High Risk were already red; authenticity Warning and
  // anomaly Medium were already amber). ──

  private donutGradient(segments: { value: number; color: string }[]): string {
    const total = segments.reduce((sum, s) => sum + s.value, 0);
    if (total <= 0) return 'conic-gradient(var(--bg-hover) 0% 100%)';
    let cursor = 0;
    const stops = segments
      .filter(s => s.value > 0)
      .map(s => {
        const start = cursor;
        cursor += (s.value / total) * 100;
        return `${s.color} ${start}% ${cursor}%`;
      });
    return `conic-gradient(${stops.join(', ')})`;
  }

  get matchingTotal(): number {
    return (this.stats.match_pass || 0) + (this.stats.match_review || 0);
  }

  get matchingDonutGradient(): string {
    return this.donutGradient([
      { value: this.stats.match_pass || 0, color: 'var(--success)' },
      { value: this.stats.match_review || 0, color: 'var(--danger)' },
    ]);
  }

  get authenticityTotal(): number {
    if (!this.authenticityQuality) return 0;
    return this.authenticityQuality.passed + this.authenticityQuality.warning + this.authenticityQuality.failed;
  }

  get authenticityDonutGradient(): string {
    if (!this.authenticityQuality) return 'conic-gradient(var(--bg-hover) 0% 100%)';
    return this.donutGradient([
      { value: this.authenticityQuality.passed, color: 'var(--success)' },
      { value: this.authenticityQuality.warning, color: 'var(--warning)' },
      { value: this.authenticityQuality.failed, color: 'var(--danger)' },
    ]);
  }

  get anomalyTotal(): number {
    if (!this.anomalyQuality) return 0;
    return this.anomalyQuality.high + this.anomalyQuality.medium + this.anomalyQuality.low;
  }

  get anomalyDonutGradient(): string {
    if (!this.anomalyQuality) return 'conic-gradient(var(--bg-hover) 0% 100%)';
    return this.donutGradient([
      { value: this.anomalyQuality.low, color: 'var(--success)' },
      { value: this.anomalyQuality.medium, color: 'var(--warning)' },
      { value: this.anomalyQuality.high, color: 'var(--danger)' },
    ]);
  }

  // ── Audit Activity Trend — last 14 days, Approved/Sent Back/Need
  // Review (Need Review replaces the old Pending series; Pending is a
  // current-state snapshot, not a "how many that day" figure, so it
  // never belonged on a per-day trend). Gradient fills via
  // ctx.createLinearGradient, same technique already used by Finance
  // Home's own upload trend chart. ──

  renderChart() {
    if (!this.timelineChartRef) return;
    if (this.chartInstance) this.chartInstance.destroy();

    const last14 = this.timeline.slice(-14);
    const labels = last14.map(t => this.formatChartDate(t.date));
    const ctx = this.timelineChartRef.nativeElement.getContext('2d');

    const fill = (hex: string, alpha: number) => {
      const gradient = ctx.createLinearGradient(0, 0, 0, 220);
      gradient.addColorStop(0, this.hexToRgba(hex, alpha));
      gradient.addColorStop(1, this.hexToRgba(hex, 0));
      return gradient;
    };

    this.chartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Approved',
            data: last14.map(t => t.approved),
            borderColor: ACTION_COLORS.approved,
            backgroundColor: fill(ACTION_COLORS.approved, 0.25),
            tension: 0.4,
            fill: true,
            pointRadius: 2,
            pointBackgroundColor: ACTION_COLORS.approved,
          },
          {
            label: 'Sent Back',
            data: last14.map(t => t.sent_back),
            borderColor: ACTION_COLORS.sent_back,
            backgroundColor: fill(ACTION_COLORS.sent_back, 0.25),
            tension: 0.4,
            fill: true,
            pointRadius: 2,
            pointBackgroundColor: ACTION_COLORS.sent_back,
          },
          {
            label: 'Need Review',
            data: last14.map(t => t.need_review),
            borderColor: ACTION_COLORS.need_review,
            backgroundColor: fill(ACTION_COLORS.need_review, 0.25),
            tension: 0.4,
            fill: true,
            pointRadius: 2,
            pointBackgroundColor: ACTION_COLORS.need_review,
          },
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { top: 4, right: 4, bottom: 0, left: 4 } },
        animation: { duration: 700, easing: 'easeOutQuart' },
        interaction: { mode: 'index' as const, intersect: false },
        plugins: {
          legend: {
            position: 'bottom' as const,
            labels: { boxWidth: 10, padding: 14, font: { size: 11 } }
          }
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 14, font: { size: 10 } } },
          y: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 }, grid: { color: 'rgba(255,255,255,0.06)' } }
        }
      }
    });
  }

  private hexToRgba(hex: string, alpha: number): string {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }

  formatChartDate(dateStr: string): string {
    return new Date(dateStr).toLocaleDateString('en-MY', { day: '2-digit', month: 'short' });
  }

  // ── Chart 1: Review Outcome Distribution (doughnut) — review_records.
  // action, period-scoped exactly like the KPI cards above since it
  // reads the same stats.approved/sent_back/need_review. ──

  renderOutcomeChart() {
    if (!this.outcomeChartRef) return;
    if (this.outcomeChartInstance) this.outcomeChartInstance.destroy();

    const ctx = this.outcomeChartRef.nativeElement.getContext('2d');
    this.outcomeChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Approved', 'Sent Back', 'Need Review'],
        datasets: [{
          data: [this.stats.approved || 0, this.stats.sent_back || 0, this.stats.need_review || 0],
          backgroundColor: [ACTION_COLORS.approved, ACTION_COLORS.sent_back, ACTION_COLORS.need_review],
          borderWidth: 0,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '68%',
        animation: { duration: 700, easing: 'easeOutQuart' },
        plugins: {
          legend: { position: 'bottom' as const, labels: { boxWidth: 10, padding: 12, font: { size: 11 } } }
        }
      }
    });
  }

  // ── Chart 2: Audit Findings Breakdown (horizontal bar, value labels)
  // — combines three-way matching REVIEW count, authenticity FAIL
  // count, and the anomaly type breakdown, all already loaded above.
  // Rendered only once every source has actually resolved, so it never
  // draws a partial/misleading breakdown while one of the three calls
  // is still in flight. ──

  private maybeRenderFindingsChart() {
    if (!this.findingsChartRef || this.isLoadingSummary || !this.authenticityQuality || !this.anomalyByType) return;
    if (this.findingsChartInstance) this.findingsChartInstance.destroy();

    const rows = [
      { label: 'Matching: Review Required', value: this.stats.match_review || 0, color: '#F45B69' },
      { label: 'Authenticity: Failed',       value: this.authenticityQuality.failed, color: '#F45B69' },
      { label: 'Anomaly: Unusual Amount',    value: this.anomalyByType.amount, color: '#F5B83D' },
      { label: 'Anomaly: Round Amount',      value: this.anomalyByType.round, color: '#F5B83D' },
      { label: 'Anomaly: Timing',            value: this.anomalyByType.weekend, color: '#F5B83D' },
      { label: 'Anomaly: Duplicate',         value: this.anomalyByType.duplicate, color: '#F5B83D' },
    ];

    const ctx = this.findingsChartRef.nativeElement.getContext('2d');
    this.findingsChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: rows.map(r => r.label),
        datasets: [{
          data: rows.map(r => r.value),
          backgroundColor: rows.map(r => r.color),
          borderRadius: 4,
          borderSkipped: false,
          barThickness: 16,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 700, easing: 'easeOutQuart' },
        layout: { padding: { right: 28 } },
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, ticks: { precision: 0, font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.06)' } },
          y: { ticks: { font: { size: 10.5 } }, grid: { display: false } }
        }
      },
      plugins: [valueLabelPlugin]
    });
  }

  // ── Chart 3: Top Vendors by Review Activity (horizontal bar) — top 5
  // vendors by reviewed transaction count, from a dedicated larger fetch
  // of the SAME audit-trail endpoint (see vendorActivityEntries above). ──

  loadVendorActivity() {
    this.isLoadingVendorActivity = true;
    const url = `${this.apiUrl}/auditor/report/audit-trail?action=all&limit=1000&offset=0`;
    this.http.get<any>(url, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.vendorActivityEntries = res.entries || [];
        this.isLoadingVendorActivity = false;
        this.cdr.detectChanges();
        setTimeout(() => this.renderVendorsChart(), 100);
      },
      error: () => {
        this.vendorActivityEntries = [];
        this.isLoadingVendorActivity = false;
        this.cdr.detectChanges();
      }
    });
  }

  get topVendors(): { vendor: string; count: number }[] {
    const counts = new Map<string, number>();
    for (const entry of this.vendorActivityEntries) {
      const vendor = entry.vendor_name || 'Unknown vendor';
      counts.set(vendor, (counts.get(vendor) || 0) + 1);
    }
    return Array.from(counts.entries())
      .map(([vendor, count]) => ({ vendor, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 5);
  }

  renderVendorsChart() {
    if (!this.vendorsChartRef) return;
    if (this.vendorsChartInstance) this.vendorsChartInstance.destroy();

    const top = this.topVendors;
    const ctx = this.vendorsChartRef.nativeElement.getContext('2d');
    this.vendorsChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: top.map(v => v.vendor),
        datasets: [{
          data: top.map(v => v.count),
          backgroundColor: '#8B9BFF',
          borderRadius: 4,
          borderSkipped: false,
          barThickness: 16,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 700, easing: 'easeOutQuart' },
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, ticks: { precision: 0, font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.06)' } },
          y: { ticks: { font: { size: 10.5 } }, grid: { display: false } }
        }
      }
    });
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
      this.currentReviewPage = 1;
      this.currentAuditTrailPage = 1;
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

  // ── Pagination, Audit Trail — over the currently-loaded entries
  // array (Load More above still fetches further backend pages into
  // entries exactly as before; this only paginates what's already
  // loaded). ──

  get totalAuditTrailPages(): number {
    return Math.max(1, Math.ceil(this.entries.length / this.auditTrailRowsPerPage));
  }

  get auditTrailPageNumbers(): number[] {
    return Array.from({ length: this.totalAuditTrailPages }, (_, i) => i + 1);
  }

  get paginatedAuditTrail(): any[] {
    const startIndex = (this.currentAuditTrailPage - 1) * this.auditTrailRowsPerPage;
    return this.entries.slice(startIndex, startIndex + this.auditTrailRowsPerPage);
  }

  goToAuditTrailPage(page: number) {
    if (page < 1 || page > this.totalAuditTrailPages) return;
    this.currentAuditTrailPage = page;
  }

  prevAuditTrailPage() {
    this.goToAuditTrailPage(this.currentAuditTrailPage - 1);
  }

  nextAuditTrailPage() {
    this.goToAuditTrailPage(this.currentAuditTrailPage + 1);
  }

  // Recent Review Activity table (Feature 5) — reuses the SAME entries
  // array already loaded for the Audit Trail below it, in a compact
  // table. No second fetch. Paginated via paginatedReviewActivity below
  // instead of a fixed row cap.
  get recentActivity(): any[] {
    return this.entries;
  }

  // ── Pagination, Recent Review Activity — over recentActivity above
  // (same source Audit Trail's own pagination reads, just tracked with
  // its own page cursor since the two tables are independent views). ──

  get totalReviewPages(): number {
    return Math.max(1, Math.ceil(this.recentActivity.length / this.reviewRowsPerPage));
  }

  get reviewPageNumbers(): number[] {
    return Array.from({ length: this.totalReviewPages }, (_, i) => i + 1);
  }

  get paginatedReviewActivity(): any[] {
    const startIndex = (this.currentReviewPage - 1) * this.reviewRowsPerPage;
    return this.recentActivity.slice(startIndex, startIndex + this.reviewRowsPerPage);
  }

  goToReviewPage(page: number) {
    if (page < 1 || page > this.totalReviewPages) return;
    this.currentReviewPage = page;
  }

  prevReviewPage() {
    this.goToReviewPage(this.currentReviewPage - 1);
  }

  nextReviewPage() {
    this.goToReviewPage(this.currentReviewPage + 1);
  }

  // Collapsed (default) view shows just the first 5 rows, no pagination
  // controls; "View All" switches to the existing 10/page paginated view.
  get displayedReviewActivity(): any[] {
    return this.reviewActivityExpanded ? this.paginatedReviewActivity : this.recentActivity.slice(0, 5);
  }

  showAllReviewActivity() {
    this.reviewActivityExpanded = true;
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
    return formatMalaysiaDateTime(ts);
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
