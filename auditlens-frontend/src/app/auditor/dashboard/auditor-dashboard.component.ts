import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

// exception_type -> stable short label for the Exception Categories
// chart. The literal type strings mirror routes/auditor.py::
// _classify_exception() exactly (mismatch / review / sent_back /
// missing_document / low_confidence) - display-only, no new
// classification. (exception_label from the API is per-instance,
// e.g. "Low OCR Confidence (72%)", so it isn't stable enough to group
// a chart axis by - this map is for the chart only.)
const EXCEPTION_TYPE_LABELS: Record<string, string> = {
  mismatch: 'Matching Mismatch',
  review: 'Review Required',
  sent_back: 'Sent Back to Finance',
  missing_document: 'Missing Documents',
  low_confidence: 'Low OCR Confidence',
};

// Enterprise V3 Phase 6 (STEP 3) — Transaction-Centric Auditor
// Workflow. Reads GET /auditor/transactions instead of the legacy
// GET /matching/queue — a merged queue of real transaction packages
// (Phase 5) AND standalone/legacy invoices never grouped into one
// (STEP 10 backward compatibility), each already carrying its own
// matching_status computed by the EXISTING, unmodified Enterprise
// Matching V2 dispatcher. No calculation happens in this component.
//
// Audit Command Centre redesign — data loading is intentionally split
// into ONE primary call (unchanged: loadQueue(), same endpoint, same
// isLoading gate, same stat computation as before this redesign) and
// FOUR secondary calls that fire in parallel alongside it, each
// rendering its own chart independently the moment its own response
// arrives — none of them block the primary KPI/table render, and none
// of them re-fire on their own (no polling/interval anywhere; each is
// called exactly once, from ngOnInit, for the lifetime of this
// component instance). Status Breakdown and the Priority Review Queue
// are DERIVED from the already-loaded transactions array rather than
// fetched separately, to avoid a duplicate/overlapping request.
@Component({
  selector: 'app-auditor-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-dashboard.component.html',
  styleUrls: ['./auditor-dashboard.component.css']
})
export class AuditorDashboardComponent implements OnInit, AfterViewInit {
  @ViewChild('trendChart') trendChartRef!: ElementRef;
  @ViewChild('volumeChart') volumeChartRef!: ElementRef;
  @ViewChild('statusChart') statusChartRef!: ElementRef;
  @ViewChild('authChart') authChartRef!: ElementRef;
  @ViewChild('exceptionChart') exceptionChartRef!: ElementRef;
  @ViewChild('riskChart') riskChartRef!: ElementRef;

  // ── Primary content (unchanged behavior) ──────────────────
  isLoading: boolean = false;
  transactions: any[] = [];

  totalRecords: number = 0;
  fullMatch: number = 0;
  needReview: number = 0;
  missingDocuments: number = 0;

  // ── Derived from the SAME transactions array (no new call) ──
  statusBreakdown = { pass: 0, review: 0, missingDoc: 0 };
  priorityItems: any[] = [];

  // Table search/filter (client-side only, over already-loaded data)
  searchText: string = '';
  statusFilter: string = 'all';

  // ── Secondary sections: independent load state, each fetched
  // exactly once in ngOnInit, none blocking the primary render ──
  reportSummaryLoaded = false;
  exceptionsLoaded = false;
  authenticityLoaded = false;
  anomalyStatsLoaded = false;

  exceptionCategories: { label: string; value: number }[] = [];
  authenticityOutcomes = { pass: 0, warning: 0, fail: 0 };
  riskDistribution = { high: 0, medium: 0, low: 0 };

  private viewReady = false;
  private trendChartInstance: any = null;
  private volumeChartInstance: any = null;
  private statusChartInstance: any = null;
  private authChartInstance: any = null;
  private exceptionChartInstance: any = null;
  private riskChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    // All 5 requests fire together, in parallel — the primary call
    // (loadQueue) is not chained behind the other 4, and none of the
    // 4 secondary calls are chained behind each other. Each is called
    // exactly once for this component's lifetime; nothing here polls
    // or re-fires on an interval.
    this.loadQueue();
    this.loadReportSummary();
    this.loadExceptions();
    this.loadAuthenticity();
    this.loadAnomalyStats();
  }

  ngAfterViewInit() {
    this.viewReady = true;
    // Any secondary call that already resolved before the view was
    // ready gets its chart drawn now; calls still in flight draw their
    // own chart later, from their own subscribe callback below.
    this.renderTrendChart();
    this.renderVolumeChart();
    this.renderStatusChart();
    this.renderAuthChart();
    this.renderExceptionChart();
    this.renderRiskChart();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── Primary: unchanged from before this redesign ──────────
  loadQueue() {
    this.isLoading = true;
    this.http.get<any[]>(`${this.apiUrl}/auditor/transactions`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.transactions   = res || [];
        this.totalRecords   = this.transactions.length;
        this.fullMatch      = this.transactions.filter((t: any) => t.matching_status === 'PASS').length;
        this.needReview     = this.transactions.filter((t: any) => t.matching_status === 'REVIEW').length;
        this.missingDocuments = this.transactions.filter((t: any) => !t.po_count || !t.gr_count).length;
        this.isLoading       = false;
        this.computeStatusBreakdown();
        this.computePriorityItems();
        this.cdr.detectChanges();
        this.renderStatusChart();
      },
      error: () => { this.isLoading = false; }
    });
  }

  private computeStatusBreakdown() {
    let pass = 0, review = 0, missingDoc = 0;
    for (const t of this.transactions) {
      if (!t.po_count || !t.gr_count) missingDoc++;
      else if (t.matching_status === 'PASS') pass++;
      else review++;
    }
    this.statusBreakdown = { pass, review, missingDoc };
  }

  private computePriorityItems() {
    const flagged = this.transactions.filter(t => t.matching_status === 'REVIEW' || !t.po_count || !t.gr_count);
    flagged.sort((a, b) => {
      const rankDiff = this.riskRank(b) - this.riskRank(a);
      if (rankDiff !== 0) return rankDiff;
      return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
    });
    this.priorityItems = flagged.slice(0, 5);
  }

  riskLevelFor(t: any): 'HIGH' | 'MEDIUM' | 'LOW' {
    const missingOne = !t.po_count || !t.gr_count;
    if (t.matching_status === 'REVIEW' && missingOne) return 'HIGH';
    if (t.matching_status === 'REVIEW') return 'MEDIUM';
    if (missingOne) return 'MEDIUM';
    return 'LOW';
  }

  private riskRank(t: any): number {
    const lvl = this.riskLevelFor(t);
    return lvl === 'HIGH' ? 2 : lvl === 'MEDIUM' ? 1 : 0;
  }

  issuesFor(t: any): string {
    const parts: string[] = [];
    if (!t.po_count) parts.push('Missing PO');
    if (!t.gr_count) parts.push('Missing GR');
    if (t.matching_status === 'REVIEW' && t.po_count && t.gr_count) parts.push('Needs Review');
    return parts.length ? parts.join(', ') : '—';
  }

  ageDays(dateStr: string): number {
    if (!dateStr) return 0;
    return Math.max(0, Math.floor((Date.now() - new Date(dateStr).getTime()) / 86400000));
  }

  pct(n: number): string {
    return this.totalRecords > 0 ? ((n / this.totalRecords) * 100).toFixed(1) : '0';
  }

  get filteredTransactions(): any[] {
    let list = this.transactions;
    if (this.statusFilter !== 'all') {
      list = list.filter(t => t.matching_status === this.statusFilter);
    }
    const q = this.searchText.trim().toLowerCase();
    if (q) {
      list = list.filter(t =>
        (t.package_name || '').toLowerCase().includes(q) ||
        (t.supplier || '').toLowerCase().includes(q)
      );
    }
    return list;
  }

  // ── Secondary: Audit Trend + Review Volume (report/summary) ──
  reportSummary: any = null;

  loadReportSummary() {
    this.http.get<any>(`${this.apiUrl}/auditor/report/summary`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.reportSummary = res;
        this.reportSummaryLoaded = true;
        this.cdr.detectChanges();
        this.renderTrendChart();
        this.renderVolumeChart();
      },
      error: () => { this.reportSummaryLoaded = true; }
    });
  }

  // ── Secondary: Exception Categories ──
  loadExceptions() {
    this.http.get<any[]>(`${this.apiUrl}/auditor/exceptions`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        const list = res || [];
        const counts: Record<string, number> = {};
        for (const e of list) {
          counts[e.exception_type] = (counts[e.exception_type] || 0) + 1;
        }
        this.exceptionCategories = Object.entries(counts)
          .map(([type, value]) => ({ label: EXCEPTION_TYPE_LABELS[type] || type, value }))
          .sort((a, b) => b.value - a.value);
        this.exceptionsLoaded = true;
        this.cdr.detectChanges();
        this.renderExceptionChart();
      },
      error: () => { this.exceptionsLoaded = true; }
    });
  }

  // ── Secondary: Authenticity Outcomes ──
  loadAuthenticity() {
    this.http.get<any[]>(`${this.apiUrl}/authenticity`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        const list = res || [];
        let pass = 0, warning = 0, fail = 0;
        for (const a of list) {
          if (a.risk_level === 'HIGH') fail++;
          else if (a.authenticity_status === 'passed') pass++;
          else warning++;
        }
        this.authenticityOutcomes = { pass, warning, fail };
        this.authenticityLoaded = true;
        this.cdr.detectChanges();
        this.renderAuthChart();
      },
      error: () => { this.authenticityLoaded = true; }
    });
  }

  // ── Secondary: Risk Distribution (anomaly severity) ──
  loadAnomalyStats() {
    this.http.get<any>(`${this.apiUrl}/anomalies/stats`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        const sev = res?.by_severity || {};
        this.riskDistribution = { high: sev.high || 0, medium: sev.medium || 0, low: sev.low || 0 };
        this.anomalyStatsLoaded = true;
        this.cdr.detectChanges();
        this.renderRiskChart();
      },
      error: () => { this.anomalyStatsLoaded = true; }
    });
  }

  goToReviewQueue() {
    this.router.navigate(['/auditor/home']);
  }

  goToRecord(txn: any) {
    if (txn.kind === 'transaction_package') {
      this.router.navigate(['/auditor/record-detail'], {
        queryParams: { document_id: txn.primary_document_id, transaction_package_id: txn.transaction_package_id }
      });
    } else {
      this.router.navigate(['/auditor/record-detail'], {
        queryParams: { document_id: txn.primary_document_id }
      });
    }
  }

  matchingStatusClass(status: string): string {
    switch (status) {
      case 'PASS':   return 'badge-approved';
      case 'REVIEW': return 'badge-review';
      case 'PARTIAL': return 'badge-resubmitted';
      default:       return 'badge-pending';
    }
  }

  matchingStatusLabel(status: string): string {
    switch (status) {
      case 'PASS':    return 'PASS';
      case 'REVIEW':  return 'REVIEW REQUIRED';
      case 'PARTIAL': return 'PARTIAL';
      default:        return 'PENDING';
    }
  }

  riskBadgeClass(level: string): string {
    if (level === 'HIGH') return 'badge-returned';
    if (level === 'MEDIUM') return 'badge-review';
    return 'badge-approved';
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  // ── Chart rendering — each guarded independently: only draws once
  // BOTH the view exists (viewReady) AND that section's own data has
  // arrived. Called from ngAfterViewInit (covers data-arrived-first)
  // and again from each load method's own callback (covers view-
  // ready-first) — whichever happens second is what actually draws. ──

  renderTrendChart() {
    if (!this.viewReady || !this.trendChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.trendChartInstance) this.trendChartInstance.destroy();

    const timeline: any[] = this.reportSummary.timeline || [];
    const recent = timeline.slice(-14); // last 14 of the 30 days, compact view
    const labels = recent.map(t => this.formatShortDate(t.date));

    const ctx = this.trendChartRef.nativeElement.getContext('2d');
    this.trendChartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Approved',
            data: recent.map(t => t.approved),
            borderColor: '#5DCAA5',
            backgroundColor: 'rgba(93, 202, 165, 0.12)',
            borderWidth: 2, pointRadius: 0, tension: 0.35, fill: true,
          },
          {
            label: 'Sent Back',
            data: recent.map(t => t.sent_back),
            borderColor: '#E5605E',
            backgroundColor: 'rgba(229, 96, 94, 0.08)',
            borderWidth: 2, pointRadius: 0, tension: 0.35, fill: true,
          },
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, position: 'top' as const, labels: { boxWidth: 8, font: { size: 10 }, padding: 8 } }
        },
        scales: {
          y: { display: false, beginAtZero: true },
          x: { display: true, grid: { display: false }, ticks: { font: { size: 9 } } }
        }
      }
    });
  }

  renderVolumeChart() {
    if (!this.viewReady || !this.volumeChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.volumeChartInstance) this.volumeChartInstance.destroy();

    const timeline: any[] = this.reportSummary.timeline || [];
    const recent = timeline.slice(-14);
    const labels = recent.map(t => this.formatShortDate(t.date));

    const ctx = this.volumeChartRef.nativeElement.getContext('2d');
    this.volumeChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data: recent.map(t => t.pending),
          backgroundColor: 'rgba(108, 79, 255, 0.7)',
          borderRadius: 3, borderSkipped: false,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { display: false, beginAtZero: true },
          x: { display: false }
        }
      }
    });
  }

  renderStatusChart() {
    if (!this.viewReady || !this.statusChartRef || this.isLoading) return;
    if (this.statusChartInstance) this.statusChartInstance.destroy();

    const s = this.statusBreakdown;
    const ctx = this.statusChartRef.nativeElement.getContext('2d');
    this.statusChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Pass', 'Review', 'Missing Document'],
        datasets: [{
          data: [s.pass, s.review, s.missingDoc],
          backgroundColor: ['#5DCAA5', '#F0A93B', '#E5605E'],
          borderWidth: 0, hoverOffset: 4,
        }]
      },
      options: {
        cutout: '70%',
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' as const, labels: { boxWidth: 8, padding: 8, font: { size: 10 } } } }
      }
    });
  }

  renderAuthChart() {
    if (!this.viewReady || !this.authChartRef || !this.authenticityLoaded) return;
    if (this.authChartInstance) this.authChartInstance.destroy();

    const a = this.authenticityOutcomes;
    const ctx = this.authChartRef.nativeElement.getContext('2d');
    this.authChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Pass', 'Warning', 'Fail'],
        datasets: [{
          data: [a.pass, a.warning, a.fail],
          backgroundColor: ['#5DCAA5', '#F0A93B', '#E5605E'],
          borderWidth: 0, hoverOffset: 4,
        }]
      },
      options: {
        cutout: '70%',
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' as const, labels: { boxWidth: 8, padding: 8, font: { size: 10 } } } }
      }
    });
  }

  renderExceptionChart() {
    if (!this.viewReady || !this.exceptionChartRef || !this.exceptionsLoaded) return;
    if (this.exceptionChartInstance) this.exceptionChartInstance.destroy();

    const cats = this.exceptionCategories;
    const ctx = this.exceptionChartRef.nativeElement.getContext('2d');
    this.exceptionChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: cats.map(c => c.label),
        datasets: [{
          data: cats.map(c => c.value),
          backgroundColor: '#8B72FF',
          borderRadius: 3, borderSkipped: false,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false, beginAtZero: true },
          y: { ticks: { font: { size: 10.5 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  renderRiskChart() {
    if (!this.viewReady || !this.riskChartRef || !this.anomalyStatsLoaded) return;
    if (this.riskChartInstance) this.riskChartInstance.destroy();

    const r = this.riskDistribution;
    const ctx = this.riskChartRef.nativeElement.getContext('2d');
    this.riskChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: ['High', 'Medium', 'Low'],
        datasets: [{
          data: [r.high, r.medium, r.low],
          backgroundColor: ['#E5605E', '#F0A93B', '#5DCAA5'],
          borderRadius: 3, borderSkipped: false,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false, beginAtZero: true },
          y: { ticks: { font: { size: 10.5 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  private formatShortDate(dateStr: string): string {
    if (!dateStr) return '';
    const d = new Date(dateStr + 'T00:00:00');
    return d.toLocaleDateString('en-US', { day: '2-digit', month: 'short' });
  }
}
