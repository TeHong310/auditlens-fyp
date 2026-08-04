import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

// findings_by_category key -> display label (routes/auditor.py's
// _build_dashboard_extras() returns exactly these 7 keys, mirroring
// the anomaly_type set (amount/round/weekend/duplicate) plus matching/
// missing-document/authenticity concepts already used elsewhere in
// this app) — display-only, no new classification.
const FINDING_CATEGORY_LABELS: Record<string, string> = {
  matching_mismatch: 'Matching Mismatch',
  missing_documents: 'Missing Documents',
  authenticity_concern: 'Authenticity Concern',
  round_amount: 'Round Amount',
  timing: 'Timing',
  duplicate: 'Duplicate',
  unusual_amount: 'Unusual Amount',
};

// Shared chart palette — richer/more varied than the app's 4 flat
// semantic tokens, used only for chart decoration (display only, no
// data/logic implication). Family grouping keeps status meaning
// intact: green/teal = success, amber/orange = warning, coral/red =
// danger, violet/blue/cyan = neutral analytics — while giving
// different datasets across the page clearly different hues.
const CHART_PALETTE = {
  violet: '#8B5CF6',
  blue: '#3B82F6',
  cyan: '#22D3EE',
  teal: '#2DD4BF',
  green: '#34D399',
  amber: '#FBBF24',
  orange: '#FB923C',
  coral: '#FB7185',
  red: '#F43F5E',
  pink: '#F472B6',
};

// KPI card accent colors — exact values specified for the dashboard
// redesign (distinct from the app's 4 flat semantic tokens/var(--...)
// used elsewhere, since these 5 cards needed their own fixed palette).
const KPI_COLORS = {
  activeCases: '#7C5CFC',
  completed:   '#55D6A9',
  needReview:  '#FFB84D',
  highRisk:    '#FF667A',
  avgTime:     '#4DA3FF',
};

// Enterprise V3 Phase 6 (STEP 3) — Transaction-Centric Auditor
// Workflow. Reads GET /auditor/transactions instead of the legacy
// GET /matching/queue — a merged queue of real transaction packages
// (Phase 5) AND standalone/legacy invoices never grouped into one
// (STEP 10 backward compatibility), each already carrying its own
// matching_status computed by the EXISTING, unmodified Enterprise
// Matching V2 dispatcher. No calculation happens in this component.
//
// Dashboard analytics redesign — data loading is TWO parallel calls:
// loadQueue() (unchanged: GET /auditor/transactions, still feeds the
// Transaction Review Queue table + Priority Review Queue, both derived
// client-side from the same transactions array) and
// loadReportSummary() (GET /auditor/report/summary, extended — see
// routes/auditor.py's _build_dashboard_extras() — to also carry every
// new KPI/chart figure this redesign needed: kpi, workload_trend,
// review_ageing, matching_outcomes, authenticity_outcomes,
// findings_by_category, vendor_ranking). The pre-redesign SEPARATE
// calls to GET /auditor/exceptions, GET /authenticity, and
// GET /anomalies/stats are gone — their data is now folded into this
// one extended report/summary response (Findings by Category already
// covers the 4 anomaly types + matching/missing-document/authenticity
// concepts the old 3 separate charts used), so this page now makes
// fewer requests, not more.
@Component({
  selector: 'app-auditor-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-dashboard.component.html',
  styleUrls: ['./auditor-dashboard.component.css']
})
export class AuditorDashboardComponent implements OnInit, AfterViewInit {
  @ViewChild('workloadChart') workloadChartRef!: ElementRef;
  @ViewChild('ageingChart') ageingChartRef!: ElementRef;
  @ViewChild('matchingChart') matchingChartRef!: ElementRef;
  @ViewChild('authChart') authChartRef!: ElementRef;
  @ViewChild('findingsChart') findingsChartRef!: ElementRef;
  @ViewChild('vendorChart') vendorChartRef!: ElementRef;

  kpiColors = KPI_COLORS;

  // ── Primary content (unchanged behavior) ──────────────────
  isLoading: boolean = false;
  transactions: any[] = [];
  totalRecords: number = 0;

  // ── Derived from the SAME transactions array (no new call) ──
  priorityItems: any[] = [];

  // ── Secondary: report/summary (KPIs + every chart below) ──
  reportSummary: any = null;
  reportSummaryLoaded = false;

  private viewReady = false;
  private workloadChartInstance: any = null;
  private ageingChartInstance: any = null;
  private matchingChartInstance: any = null;
  private authChartInstance: any = null;
  private findingsChartInstance: any = null;
  private vendorChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    // Both requests fire together, in parallel — neither is chained
    // behind the other. Each is called exactly once for this
    // component's lifetime; nothing here polls or re-fires on an
    // interval.
    this.loadQueue();
    this.loadReportSummary();
  }

  ngAfterViewInit() {
    this.viewReady = true;
    // Whichever call already resolved before the view was ready gets
    // its chart drawn now; a call still in flight draws its own chart
    // later, from its own subscribe callback below.
    this.renderWorkloadChart();
    this.renderAgeingChart();
    this.renderMatchingChart();
    this.renderAuthChart();
    this.renderFindingsChart();
    this.renderVendorChart();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── Primary: Transaction Review Queue + Priority Review Queue ──
  loadQueue() {
    this.isLoading = true;
    this.http.get<any[]>(`${this.apiUrl}/auditor/transactions`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.transactions = res || [];
        this.totalRecords = this.transactions.length;
        this.isLoading = false;
        this.computePriorityItems();
        this.cdr.detectChanges();
      },
      error: () => { this.isLoading = false; }
    });
  }

  // Priority Review Queue ranking (dashboard redesign) — prioritised,
  // in order: High-risk findings > Failed authenticity > Missing
  // documents > Returned/resubmitted cases > Oldest active records.
  // Only ACTIVE cases (not yet at a final approved/closed decision)
  // are eligible at all — a case that has already been decided has no
  // place in a queue of work still to do.
  private computePriorityItems() {
    const active = this.transactions.filter(t =>
      t.latest_review_action !== 'approved' && t.latest_review_action !== 'closed'
    );
    const flagged = active.filter(t =>
      this.riskLevelFor(t) === 'HIGH' ||
      t.authenticity_outcome === 'risk_detected' ||
      !t.po_count || !t.gr_count ||
      t.latest_review_action === 'returned' || t.latest_review_action === 'resubmitted' ||
      t.workflow_status === 'NEED REVIEW' || t.has_material_finding
    );
    flagged.sort((a, b) => {
      const rankDiff = this.priorityScore(b) - this.priorityScore(a);
      if (rankDiff !== 0) return rankDiff;
      // Final tiebreak: oldest active record first.
      return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime();
    });
    this.priorityItems = flagged.slice(0, 5);
  }

  private priorityScore(t: any): number {
    let score = 0;
    if (this.riskLevelFor(t) === 'HIGH') score += 1000;
    if (t.authenticity_outcome === 'risk_detected') score += 500;
    if (!t.po_count || !t.gr_count) score += 200;
    if (t.latest_review_action === 'returned' || t.latest_review_action === 'resubmitted') score += 100;
    return score;
  }

  // Risk here considers the Auditor's own decision and open anomalies,
  // not only the matching result — a clean PASS with a pending Need
  // Review decision or a material anomaly is never read as LOW. Mirrors
  // routes/auditor.py's _package_risk_level() exactly, so the High-Risk
  // Findings KPI card (backend-computed) never disagrees with this same
  // badge shown in the queue/priority tables.
  riskLevelFor(t: any): 'HIGH' | 'MEDIUM' | 'LOW' {
    const missingOne = !t.po_count || !t.gr_count;
    if (t.anomaly_risk_level === 'HIGH') return 'HIGH';
    if (t.matching_status === 'REVIEW' && missingOne) return 'HIGH';
    if (t.matching_status === 'REVIEW') return 'MEDIUM';
    if (t.workflow_status === 'NEED REVIEW' || t.has_material_finding) return 'MEDIUM';
    if (missingOne) return 'MEDIUM';
    return 'LOW';
  }

  issuesFor(t: any): string {
    const parts: string[] = [];
    if (!t.po_count) parts.push('Missing PO');
    if (!t.gr_count) parts.push('Missing GR');
    if (t.authenticity_outcome === 'risk_detected') parts.push('Failed Authenticity');
    if (t.latest_review_action === 'returned') parts.push('Sent Back');
    else if (t.latest_review_action === 'resubmitted') parts.push('Resubmitted');
    if (t.workflow_status === 'NEED REVIEW') parts.push('Needs Review (Auditor)');
    else if (t.matching_status === 'REVIEW' && t.po_count && t.gr_count) parts.push('Needs Review');
    if (t.has_material_finding) parts.push('Pending Anomaly');
    return parts.length ? parts.join(', ') : '—';
  }

  ageDays(dateStr: string): number {
    if (!dateStr) return 0;
    return Math.max(0, Math.floor((Date.now() - new Date(dateStr).getTime()) / 86400000));
  }

  // Related Docs column — joins the reference numbers already returned
  // by GET /auditor/transactions (invoice_numbers/po_numbers/gr_numbers)
  // so an auditor can spot, e.g., multiple invoices sharing one PO
  // just by scanning the column. No new data fetching.
  relatedDocLabel(numbers: string[] | undefined): string {
    return numbers && numbers.length > 0 ? numbers.join(', ') : '-';
  }

  // Compact "at a glance" slice for Auditor Home only (most recent 5) —
  // full search/filter/browse already lives on the dedicated Review
  // Queue page (/auditor/review-queue, unchanged), which this links to.
  get recentTransactions(): any[] {
    return [...this.transactions]
      .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime())
      .slice(0, 5);
  }

  // ── Secondary: report/summary — KPIs + every chart below ──
  loadReportSummary() {
    this.http.get<any>(`${this.apiUrl}/auditor/report/summary`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.reportSummary = res;
        this.reportSummaryLoaded = true;
        this.cdr.detectChanges();
        this.renderWorkloadChart();
        this.renderAgeingChart();
        this.renderMatchingChart();
        this.renderAuthChart();
        this.renderFindingsChart();
        this.renderVendorChart();
      },
      error: () => { this.reportSummaryLoaded = true; }
    });
  }

  // ── KPI card helpers ──

  get kpi(): any {
    return this.reportSummary?.kpi || null;
  }

  // "Show '—' when there is insufficient review-time data" — null from
  // the backend means no package was approved/returned in the last 30
  // days to measure a duration from at all.
  formatReviewTime(minutes: number | null | undefined): string {
    if (minutes === null || minutes === undefined) return '—';
    if (minutes < 60) return `${minutes} min`;
    const hours = minutes / 60;
    if (hours < 24) return `${hours.toFixed(1)} hrs`;
    return `${(hours / 24).toFixed(1)} days`;
  }

  // ── Matching Outcomes donut — center label ──

  get matchingOutcomesTotal(): number {
    const m = this.reportSummary?.matching_outcomes;
    if (!m) return 0;
    return (m.full_match || 0) + (m.review_required || 0) + (m.mismatch || 0) + (m.missing_documents || 0);
  }

  // ── Findings by Category ──

  get findingsList(): { label: string; value: number }[] {
    const f = this.reportSummary?.findings_by_category;
    if (!f) return [];
    return Object.entries(FINDING_CATEGORY_LABELS)
      .map(([key, label]) => ({ label, value: f[key] || 0 }))
      .filter(c => c.value > 0)
      .sort((a, b) => b.value - a.value);
  }

  get findingsTotal(): number {
    return this.findingsList.reduce((sum, c) => sum + c.value, 0);
  }

  // ── Vendor Finding Ranking ──

  get vendorRanking(): { vendor: string; count: number }[] {
    return this.reportSummary?.vendor_ranking || [];
  }

  openReviewQueue() {
    this.router.navigate(['/auditor/review-queue']);
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

  // ── Chart rendering — each guarded independently: only draws once
  // BOTH the view exists (viewReady) AND the report/summary response
  // has arrived. Called from ngAfterViewInit (covers data-arrived-
  // first) and again from loadReportSummary()'s own callback (covers
  // view-ready-first) — whichever happens second is what actually
  // draws. ──

  renderWorkloadChart() {
    if (!this.viewReady || !this.workloadChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.workloadChartInstance) this.workloadChartInstance.destroy();

    const trend: any[] = this.reportSummary.workload_trend || [];
    const labels = trend.map(t => this.formatShortDate(t.date));

    const ctx = this.workloadChartRef.nativeElement.getContext('2d');
    this.workloadChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            type: 'bar', label: 'New Cases',
            data: trend.map(t => t.new_cases),
            backgroundColor: CHART_PALETTE.violet, borderRadius: 4, borderSkipped: false, order: 3,
          },
          {
            type: 'bar', label: 'Completed Reviews',
            data: trend.map(t => t.completed),
            backgroundColor: CHART_PALETTE.green, borderRadius: 4, borderSkipped: false, order: 3,
          },
          {
            type: 'bar', label: 'Sent Back',
            data: trend.map(t => t.sent_back),
            backgroundColor: CHART_PALETTE.coral, borderRadius: 4, borderSkipped: false, order: 3,
          },
          {
            type: 'line', label: 'Pending Balance',
            data: trend.map(t => t.pending_balance),
            borderColor: CHART_PALETTE.blue, backgroundColor: 'rgba(59, 130, 246, 0.12)',
            borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 4,
            pointBackgroundColor: CHART_PALETTE.blue,
            tension: 0.35, fill: true, order: 1,
          },
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index' as const, intersect: false },
        plugins: {
          legend: { display: true, position: 'top' as const, labels: { boxWidth: 8, font: { size: 10 }, padding: 6 } }
        },
        scales: {
          y: { display: false, beginAtZero: true },
          x: { display: true, grid: { display: false }, ticks: { font: { size: 9 } } }
        }
      }
    });
  }

  renderAgeingChart() {
    if (!this.viewReady || !this.ageingChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.ageingChartInstance) this.ageingChartInstance.destroy();

    const a = this.reportSummary.review_ageing || { under_1d: 0, d1_3: 0, d4_7: 0, over_7d: 0 };
    const ctx = this.ageingChartRef.nativeElement.getContext('2d');
    this.ageingChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: ['< 1 day', '1–3 days', '4–7 days', '> 7 days'],
        datasets: [{
          data: [a.under_1d, a.d1_3, a.d4_7, a.over_7d],
          backgroundColor: [CHART_PALETTE.green, CHART_PALETTE.cyan, CHART_PALETTE.amber, CHART_PALETTE.red],
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
          y: { ticks: { font: { size: 10.5 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  renderMatchingChart() {
    if (!this.viewReady || !this.matchingChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.matchingChartInstance) this.matchingChartInstance.destroy();

    const m = this.reportSummary.matching_outcomes || { full_match: 0, review_required: 0, mismatch: 0, missing_documents: 0 };
    const ctx = this.matchingChartRef.nativeElement.getContext('2d');
    this.matchingChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Full Match', 'Review Required', 'Mismatch', 'Missing Documents'],
        datasets: [{
          data: [m.full_match, m.review_required, m.mismatch, m.missing_documents],
          backgroundColor: [CHART_PALETTE.green, CHART_PALETTE.amber, CHART_PALETTE.coral, CHART_PALETTE.red],
          borderWidth: 0, borderRadius: 6, spacing: 3, hoverOffset: 6,
        }]
      },
      options: {
        cutout: '68%',
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' as const, labels: { boxWidth: 8, padding: 6, font: { size: 10 } } } }
      }
    });
  }

  renderAuthChart() {
    if (!this.viewReady || !this.authChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.authChartInstance) this.authChartInstance.destroy();

    // Same segmented-ring engine as before — only the 3 categories/
    // data source changed (package-level authenticity_outcomes from
    // report/summary, not a raw per-document GET /authenticity call).
    const a = this.reportSummary.authenticity_outcomes || { passed: 0, review_required: 0, risk_detected: 0 };
    const ctx = this.authChartRef.nativeElement.getContext('2d');
    this.authChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Passed', 'Review Required', 'Risk Detected'],
        datasets: [{
          data: [a.passed, a.review_required, a.risk_detected],
          backgroundColor: [CHART_PALETTE.teal, CHART_PALETTE.orange, CHART_PALETTE.pink],
          borderWidth: 0, borderRadius: 6, spacing: 3, hoverOffset: 6,
        }]
      },
      options: {
        cutout: '65%',
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' as const, labels: { boxWidth: 8, padding: 6, font: { size: 10.5 } } } }
      }
    });
  }

  renderFindingsChart() {
    if (!this.viewReady || !this.findingsChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.findingsChartInstance) this.findingsChartInstance.destroy();
    if (this.findingsTotal === 0) return; // compact success state shown instead — nothing to draw

    const cats = this.findingsList;
    const categoryColors = [
      CHART_PALETTE.violet, CHART_PALETTE.cyan, CHART_PALETTE.blue, CHART_PALETTE.amber,
      CHART_PALETTE.pink, CHART_PALETTE.teal, CHART_PALETTE.orange,
    ];
    const ctx = this.findingsChartRef.nativeElement.getContext('2d');
    this.findingsChartInstance = new Chart(ctx, {
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
          y: { ticks: { font: { size: 10.5 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  renderVendorChart() {
    if (!this.viewReady || !this.vendorChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.vendorChartInstance) this.vendorChartInstance.destroy();
    const list = this.vendorRanking;
    if (list.length === 0) return; // compact success state shown instead — nothing to draw

    const categoryColors = [CHART_PALETTE.coral, CHART_PALETTE.amber, CHART_PALETTE.violet, CHART_PALETTE.blue, CHART_PALETTE.cyan];
    const ctx = this.vendorChartRef.nativeElement.getContext('2d');
    this.vendorChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: list.map(v => v.vendor),
        datasets: [{
          data: list.map(v => v.count),
          backgroundColor: list.map((_, i) => categoryColors[i % categoryColors.length]),
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
