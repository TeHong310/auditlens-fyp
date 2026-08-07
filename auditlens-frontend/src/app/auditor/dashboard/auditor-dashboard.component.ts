import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
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
  imports: [CommonModule],
  templateUrl: './auditor-dashboard.component.html',
  styleUrls: ['./auditor-dashboard.component.css']
})
export class AuditorDashboardComponent implements OnInit, AfterViewInit {
  @ViewChild('trendChart') trendChartRef!: ElementRef;
  @ViewChild('volumeChart') volumeChartRef!: ElementRef;
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

  // ── Secondary sections: independent load state, each fetched
  // exactly once in ngOnInit, none blocking the primary render ──
  reportSummaryLoaded = false;
  exceptionsLoaded = false;
  authenticityLoaded = false;
  anomalyStatsLoaded = false;

  exceptionCategories: { label: string; value: number }[] = [];
  authenticityOutcomes = { pass: 0, warning: 0, fail: 0 };
  // Anomaly TYPE breakdown (not severity) — powers the Risk
  // Distribution radar chart. Read from the SAME /anomalies/stats
  // response already fetched below (its by_type field was simply
  // unused before this redesign) — no new request.
  anomalyTypeDistribution = { amount: 0, round: 0, weekend: 0, duplicate: 0 };

  private viewReady = false;
  private trendChartInstance: any = null;
  private volumeChartInstance: any = null;
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
        // Counts the Auditor's own Need Review DECISION (latest
        // review_records action), never the matching engine's own
        // REVIEW verdict — those are two different signals (see Status
        // Breakdown below, still purely matching-based). A transaction
        // later approved/sent back naturally drops out, since
        // workflow_status always reflects the LATEST action.
        this.needReview     = this.transactions.filter((t: any) => t.workflow_status === 'NEED REVIEW').length;
        this.missingDocuments = this.transactions.filter((t: any) => !t.po_count || !t.gr_count).length;
        this.isLoading       = false;
        this.computeStatusBreakdown();
        this.computePriorityItems();
        this.cdr.detectChanges();
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
    // Need Review and a pending Medium/High anomaly both surface a
    // transaction here even when matching itself came back PASS and
    // both documents are present — the whole point of separating Audit
    // Decision/risk from the matching result.
    const flagged = this.transactions.filter(t =>
      t.matching_status === 'REVIEW' || !t.po_count || !t.gr_count ||
      t.workflow_status === 'NEED REVIEW' || t.has_material_finding
    );
    flagged.sort((a, b) => {
      const rankDiff = this.riskRank(b) - this.riskRank(a);
      if (rankDiff !== 0) return rankDiff;
      return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
    });
    this.priorityItems = flagged.slice(0, 4);
  }

  // Risk here considers the Auditor's own decision and open anomalies,
  // not only the matching result — a clean PASS with a pending Need
  // Review decision or a material anomaly is never read as LOW.
  riskLevelFor(t: any): 'HIGH' | 'MEDIUM' | 'LOW' {
    const missingOne = !t.po_count || !t.gr_count;
    if (t.anomaly_risk_level === 'HIGH') return 'HIGH';
    if (t.matching_status === 'REVIEW' && missingOne) return 'HIGH';
    if (t.matching_status === 'REVIEW') return 'MEDIUM';
    if (t.workflow_status === 'NEED REVIEW' || t.has_material_finding) return 'MEDIUM';
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

  pct(n: number): string {
    return this.totalRecords > 0 ? ((n / this.totalRecords) * 100).toFixed(1) : '0';
  }

  // Compact "at a glance" slice for Auditor Home only (most recent 5) —
  // full search/filter/browse already lives on the dedicated Review
  // Queue page (/auditor/review-queue, unchanged), which this links to.
  get recentTransactions(): any[] {
    return [...this.transactions]
      .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime())
      .slice(0, 5);
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

  // ── Secondary: Risk Distribution (anomaly type — radar) ──
  loadAnomalyStats() {
    this.http.get<any>(`${this.apiUrl}/anomalies/stats`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        const byType = res?.by_type || {};
        this.anomalyTypeDistribution = {
          amount: byType.amount || 0,
          round: byType.round || 0,
          weekend: byType.weekend || 0,
          duplicate: byType.duplicate || 0,
        };
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
  // BOTH the view exists (viewReady) AND that section's own data has
  // arrived. Called from ngAfterViewInit (covers data-arrived-first)
  // and again from each load method's own callback (covers view-
  // ready-first) — whichever happens second is what actually draws. ──

  // Audit Decision Trend — a fixed rolling 7-day window (the most
  // recent 7 of the 30 days GET /auditor/report/summary already
  // returns), every day shown regardless of activity — a day with no
  // decisions simply renders as zero-height bars, rather than being
  // dropped from the axis entirely (which, with only active dates
  // shown, made bars read as oversized blocks instead of a normal
  // enterprise trend chart).
  get trendDays(): any[] {
    const timeline: any[] = this.reportSummary?.timeline || [];
    return timeline.slice(-7);
  }

  get trendHasActivity(): boolean {
    return this.trendDays.some((t: any) => (t.approved || 0) + (t.need_review || 0) + (t.sent_back || 0) > 0);
  }

  // Grouped bar chart only (Approved / Need Review / Sent Back side-by-
  // side per date) — no line overlay. Both the earlier "Total
  // Reviewed" (same-day sum) and "Cumulative Reviews" (running sum)
  // lines ended up competing visually with the bars rather than adding
  // a clear signal; "Total decisions" is still surfaced, just in the
  // tooltip instead of as a plotted series.
  renderTrendChart() {
    if (!this.viewReady || !this.trendChartRef || !this.reportSummaryLoaded || !this.reportSummary) return;
    if (this.trendChartInstance) this.trendChartInstance.destroy();
    if (!this.trendHasActivity) return; // canvas isn't even in the DOM (*ngIf) in this case

    const days = this.trendDays;
    const labels = days.map(t => this.formatShortDate(t.date));
    const approved = days.map(t => t.approved || 0);
    const needReview = days.map(t => t.need_review || 0);
    const sentBack = days.map(t => t.sent_back || 0);
    // Total decisions per day — each review_records row has exactly one
    // action, counted once via the backend's GROUP BY day/action, so
    // this is a plain sum of 3 disjoint categories, never a double
    // count. Used by the tooltip only, not plotted as its own series.
    const totalDecisions = days.map((_: any, i: number) => approved[i] + needReview[i] + sentBack[i]);
    // suggestedMax: a tight-but-breathing-room ceiling so a quiet day
    // (max count of 1-2) still reads as a substantial bar instead of a
    // sliver lost in a tall, mostly-empty axis — floored at 4 so even
    // an all-zero-or-one window doesn't look cramped, and always at
    // least 1 above the tallest bar so it never touches the top edge.
    const maxCount = Math.max(1, ...approved, ...needReview, ...sentBack);
    const suggestedMax = Math.max(maxCount + 1, 4);

    const ctx = this.trendChartRef.nativeElement.getContext('2d');

    // Chart.js's own per-element "delay" animation (no extra library, no
    // manual setInterval/looping) — staggers each bar left to right so
    // they grow upward from zero rather than appearing instantly.
    // One-shot: this method always destroys + recreates the Chart
    // instance rather than calling .update(), so a fresh render with
    // new data re-plays it; Chart.js never repeats an animation on its
    // own. Respects prefers-reduced-motion by collapsing to an instant
    // render.
    const reducedMotion = typeof window !== 'undefined' && !!window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const animation: any = reducedMotion
      ? { duration: 0 }
      : {
          duration: 900,
          easing: 'easeOutQuart',
          delay: (context: any) => {
            if (context.type === 'data' && context.mode === 'default' && !context.dropped) {
              context.dropped = true;
              return context.dataIndex * 35 + context.datasetIndex * 70;
            }
            return 0;
          },
        };

    this.trendChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            type: 'bar', label: 'Approved', data: approved,
            backgroundColor: CHART_PALETTE.teal,
            borderRadius: 3, borderSkipped: false,
            // Slimmer bars with more breathing room between them than
            // Chart.js's defaults (0.8/0.9) — each day's 3-bar group
            // occupies 55% of its label's width, and each bar within
            // that group occupies 75% of its own slot.
            categoryPercentage: 0.55, barPercentage: 0.75,
          },
          {
            type: 'bar', label: 'Need Review', data: needReview,
            backgroundColor: CHART_PALETTE.amber,
            borderRadius: 3, borderSkipped: false,
            categoryPercentage: 0.55, barPercentage: 0.75,
          },
          {
            type: 'bar', label: 'Sent Back', data: sentBack,
            backgroundColor: CHART_PALETTE.coral,
            borderRadius: 3, borderSkipped: false,
            categoryPercentage: 0.55, barPercentage: 0.75,
          },
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation,
        interaction: { mode: 'index' as const, intersect: false },
        plugins: {
          legend: { display: true, position: 'top' as const, labels: { boxWidth: 7, font: { size: 9.5 }, padding: 4 } },
          tooltip: {
            callbacks: {
              title: (items: any[]) => items[0]?.label || '',
              label: (item: any) => `${item.dataset.label}: ${item.formattedValue}`,
              afterBody: (items: any[]) => [`Total decisions: ${totalDecisions[items[0]?.dataIndex ?? 0]}`],
            },
            padding: 8,
            titleFont: { size: 11, weight: 'bold' as const },
            bodyFont: { size: 10.5 },
          },
        },
        scales: {
          x: { display: true, grid: { display: false }, ticks: { font: { size: 9 } } },
          y: { display: false, beginAtZero: true, suggestedMax },
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
    const barGradient = ctx.createLinearGradient(0, 0, 0, 100);
    barGradient.addColorStop(0, CHART_PALETTE.violet);
    barGradient.addColorStop(1, CHART_PALETTE.blue);

    this.volumeChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data: recent.map(t => t.pending),
          backgroundColor: barGradient,
          hoverBackgroundColor: CHART_PALETTE.cyan,
          borderRadius: 5, borderSkipped: false,
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

  // Status Breakdown's 3 mini radial rings are pure CSS (conic-gradient),
  // bound directly to statusBreakdown/totalRecords in the template — no
  // canvas/Chart.js instance needed, so they update reactively with
  // change detection like any other template expression.
  ringGradient(value: number, color: string): string {
    const percent = this.totalRecords > 0 ? (value / this.totalRecords) * 100 : 0;
    return `conic-gradient(${color} 0% ${percent}%, var(--bg-hover) ${percent}% 100%)`;
  }

  renderAuthChart() {
    if (!this.viewReady || !this.authChartRef || !this.authenticityLoaded) return;
    if (this.authChartInstance) this.authChartInstance.destroy();

    // Segmented ring — same doughnut engine as Status Breakdown, but
    // with spacing + rounded segment caps, so the two donuts on the
    // page read as visually distinct chart types rather than repeats.
    // Deliberately a different shade set from Status Breakdown (teal/
    // orange/pink-red vs its green/amber/coral) — same semantic family
    // per color, but visually distinct dataset-to-dataset across the page.
    const a = this.authenticityOutcomes;
    const ctx = this.authChartRef.nativeElement.getContext('2d');
    this.authChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Pass', 'Warning', 'Fail'],
        datasets: [{
          data: [a.pass, a.warning, a.fail],
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

  renderExceptionChart() {
    if (!this.viewReady || !this.exceptionChartRef || !this.exceptionsLoaded) return;
    if (this.exceptionChartInstance) this.exceptionChartInstance.destroy();

    // Categorical palette — these are different exception TYPES, not a
    // severity ranking, so a varied hue per bar (rather than one flat
    // color) reads more clearly and matches the richer dashboard style.
    const categoryColors = [CHART_PALETTE.violet, CHART_PALETTE.cyan, CHART_PALETTE.blue, CHART_PALETTE.amber, CHART_PALETTE.pink];
    const cats = this.exceptionCategories;
    const ctx = this.exceptionChartRef.nativeElement.getContext('2d');
    this.exceptionChartInstance = new Chart(ctx, {
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

  renderRiskChart() {
    if (!this.viewReady || !this.riskChartRef || !this.anomalyStatsLoaded) return;
    if (this.riskChartInstance) this.riskChartInstance.destroy();

    // Polar area — a colorful, clearly-readable alternative to a radar
    // for 4 categorical values (each anomaly TYPE gets its own hue and
    // slice size shows its count), same by_type data as before.
    const t = this.anomalyTypeDistribution;
    const ctx = this.riskChartRef.nativeElement.getContext('2d');
    this.riskChartInstance = new Chart(ctx, {
      type: 'polarArea',
      data: {
        labels: ['Amount', 'Round Number', 'Weekend', 'Duplicate'],
        datasets: [{
          data: [t.amount, t.round, t.weekend, t.duplicate],
          backgroundColor: [
            'rgba(139, 92, 246, 0.75)',
            'rgba(34, 211, 238, 0.75)',
            'rgba(251, 191, 36, 0.75)',
            'rgba(244, 114, 182, 0.75)',
          ],
          borderColor: '#14151E',
          borderWidth: 2,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, position: 'bottom' as const, labels: { boxWidth: 8, padding: 6, font: { size: 10 } } }
        },
        scales: {
          r: {
            beginAtZero: true,
            ticks: { display: false, backdropColor: 'transparent' },
            grid: { color: 'rgba(255,255,255,0.08)' },
          }
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
