import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

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
// into ONE primary call (loadQueue(), same endpoint, same isLoading
// gate) and 2 secondary calls (report/summary for Audit Decision
// Trend, authenticity for Authenticity Outcomes) that fire in parallel
// alongside it — none of them block the primary KPI/table render, and
// none of them re-fire on their own (no polling/interval anywhere;
// each is called exactly once, from ngOnInit, for the lifetime of this
// component instance). Status Breakdown, the Priority Review Queue,
// Review Workload, Audit Risk Distribution, and Top Risk Suppliers are
// all DERIVED from the already-loaded transactions array (Top Risk
// Suppliers also reads the authenticity call's own per-vendor result)
// rather than fetched separately.
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
  @ViewChild('riskBarChart') riskBarChartRef!: ElementRef;
  @ViewChild('topSuppliersChart') topSuppliersChartRef!: ElementRef;

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
  authenticityLoaded = false;

  authenticityOutcomes = { pass: 0, warning: 0, fail: 0 };
  // Per-vendor authenticity FAIL counts (risk_level HIGH), from the SAME
  // /authenticity call above — feeds Top Risk Suppliers' "Authenticity
  // failure: +3" scoring component. No second call.
  authenticityFailBySupplier: Map<string, number> = new Map();

  private viewReady = false;
  private trendChartInstance: any = null;
  private volumeChartInstance: any = null;
  private authChartInstance: any = null;
  private riskBarChartInstance: any = null;
  private topSuppliersChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    // 3 requests fire together, in parallel — none chained behind
    // another. Each is called exactly once for this component's
    // lifetime; nothing here polls or re-fires on an interval. Review
    // Workload / Audit Risk Distribution are derived from loadQueue()'s
    // own transactions array; Top Risk Suppliers reads both that array
    // AND the authenticity call's per-vendor result — the earlier
    // separate GET /auditor/exceptions and GET /anomalies/stats calls
    // are gone, since nothing on this page reads their data anymore.
    this.loadQueue();
    this.loadReportSummary();
    this.loadAuthenticity();
  }

  ngAfterViewInit() {
    this.viewReady = true;
    // Any secondary call that already resolved before the view was
    // ready gets its chart drawn now; calls still in flight draw their
    // own chart later, from their own subscribe callback below.
    this.renderTrendChart();
    this.renderVolumeChart();
    this.renderAuthChart();
    this.renderRiskBarChart();
    this.renderTopSuppliersChart();
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
        this.renderVolumeChart();
        this.renderRiskBarChart();
        this.renderTopSuppliersChart();
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

  // ── Secondary: Audit Decision Trend (report/summary) ──
  reportSummary: any = null;

  loadReportSummary() {
    this.http.get<any>(`${this.apiUrl}/auditor/report/summary`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.reportSummary = res;
        this.reportSummaryLoaded = true;
        this.cdr.detectChanges();
        this.renderTrendChart();
      },
      error: () => { this.reportSummaryLoaded = true; }
    });
  }

  // ── Secondary: Authenticity Outcomes — feeds the Authenticity
  // Outcomes chart below (unchanged), and ALSO builds a per-vendor FAIL
  // count from the SAME rows (authenticityFailBySupplier) for Top Risk
  // Suppliers' scoring — no second call. renderTopSuppliersChart() is
  // re-triggered here too, since that chart depends on BOTH this data
  // and the transactions array from loadQueue(); whichever resolves
  // second is what actually draws it. ──
  loadAuthenticity() {
    this.http.get<any[]>(`${this.apiUrl}/authenticity`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        const list = res || [];
        let pass = 0, warning = 0, fail = 0;
        const failBySupplier = new Map<string, number>();
        for (const a of list) {
          if (a.risk_level === 'HIGH') {
            fail++;
            const vendor = a.vendor_name || 'Unknown supplier';
            failBySupplier.set(vendor, (failBySupplier.get(vendor) || 0) + 1);
          }
          else if (a.authenticity_status === 'passed') pass++;
          else warning++;
        }
        this.authenticityOutcomes = { pass, warning, fail };
        this.authenticityFailBySupplier = failBySupplier;
        this.authenticityLoaded = true;
        this.cdr.detectChanges();
        this.renderAuthChart();
        this.renderTopSuppliersChart();
      },
      error: () => { this.authenticityLoaded = true; }
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
    // suggestedMax: just 1 unit above the tallest bar (floored at 2,
    // down from 4) so bars actually use the canvas's now-much-taller
    // (270px) height instead of a large forced ceiling leaving them
    // looking short — still enough headroom that the tallest bar's own
    // rounded top never touches the very top edge.
    const maxCount = Math.max(1, ...approved, ...needReview, ...sentBack);
    const suggestedMax = Math.max(maxCount + 1, 2);

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
            // Each day's 3-bar group occupies 65% of its label's
            // width, and each bar within that group occupies 82% of
            // its own slot — noticeably slimmer than Chart.js's
            // defaults (0.8/0.9) so the 3 categories stay readable
            // side by side, but larger than this chart's earlier 55%/
            // 75% now that its canvas has more vertical room to match.
            categoryPercentage: 0.65, barPercentage: 0.82,
          },
          {
            type: 'bar', label: 'Need Review', data: needReview,
            backgroundColor: CHART_PALETTE.amber,
            borderRadius: 3, borderSkipped: false,
            categoryPercentage: 0.65, barPercentage: 0.82,
          },
          {
            type: 'bar', label: 'Sent Back', data: sentBack,
            backgroundColor: CHART_PALETTE.coral,
            borderRadius: 3, borderSkipped: false,
            categoryPercentage: 0.65, barPercentage: 0.82,
          },
        ]
      },
      options: {
        responsive: true,
        // maintainAspectRatio:false means the canvas always fills its
        // CSS container (chart-body-trend, 270px) in both dimensions —
        // aspectRatio has no active sizing effect while that's false,
        // but is set here to match the requested config explicitly.
        maintainAspectRatio: false,
        aspectRatio: 3.5,
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

  // Shared "grow from zero, staggered" load animation for the bar
  // charts redesigned here (Review Workload / Audit Risk Distribution /
  // Top Risk Suppliers) — the same Chart.js delay-based approach Audit
  // Decision Trend already uses, duplicated locally rather than
  // factored out, since that chart is explicitly out of scope for this
  // change and shouldn't be touched to share it.
  private chartLoadAnimation(): any {
    const reducedMotion = typeof window !== 'undefined' && !!window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reducedMotion) return { duration: 0 };
    return {
      duration: 900,
      easing: 'easeOutQuart',
      delay: (context: any) => {
        if (context.type === 'data' && context.mode === 'default' && !context.dropped) {
          context.dropped = true;
          return context.dataIndex * 60 + context.datasetIndex * 80;
        }
        return 0;
      },
    };
  }

  // Review Workload — current auditor workload distribution (a live
  // snapshot of the SAME transactions array the queue table/Priority
  // Queue already use, not a date-range report), mutually-exclusive by
  // each package's own latest_review_action: never reviewed or
  // resubmitted-awaiting-a-look counts as Pending Review.
  get workloadDistribution() {
    let pending = 0, needReview = 0, completed = 0, sentBack = 0;
    for (const t of this.transactions) {
      const action = t.latest_review_action;
      if (action === 'approved' || action === 'closed') completed++;
      else if (action === 'returned') sentBack++;
      else if (action === 'need_review') needReview++;
      else pending++;
    }
    return { pending, needReview, completed, sentBack };
  }

  renderVolumeChart() {
    if (!this.viewReady || !this.volumeChartRef || this.isLoading) return;
    if (this.volumeChartInstance) this.volumeChartInstance.destroy();

    const w = this.workloadDistribution;
    const cats = [
      { label: 'Pending Review', value: w.pending, color: CHART_PALETTE.blue },
      { label: 'Need Review', value: w.needReview, color: CHART_PALETTE.amber },
      { label: 'Completed', value: w.completed, color: CHART_PALETTE.teal },
      { label: 'Sent Back', value: w.sentBack, color: CHART_PALETTE.coral },
    ];

    const ctx = this.volumeChartRef.nativeElement.getContext('2d');
    this.volumeChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: cats.map(c => c.label),
        datasets: [{
          data: cats.map(c => c.value),
          backgroundColor: cats.map(c => c.color),
          borderRadius: 4, borderSkipped: false,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        animation: this.chartLoadAnimation(),
        plugins: { legend: { display: false } },
        scales: {
          x: { display: false, beginAtZero: true },
          y: { ticks: { font: { size: 10.5 }, color: '#E6E7EE' }, grid: { display: false } }
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
        // Smaller hole (was 65%) so the ring itself reads as more
        // compact/solid rather than a big thin band, combined with a
        // smaller overall radius (was 72%) shrinking the whole ring
        // within its now also-smaller, capped chart-body-donut (160px)
        // container — chart-body-donut's own margin:auto centers it.
        cutout: '55%',
        radius: '58%',
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom' as const, labels: { boxWidth: 8, padding: 6, font: { size: 10.5 } } } }
      }
    });
  }

  // Audit Risk Distribution — High / Medium / Low count of reviewed
  // transactions, reusing riskLevelFor()/riskDistribution below (the
  // SAME existing transaction risk classification already driving the
  // queue table's/Priority Queue's risk badges) rather than a second
  // calculation. Fixed High → Medium → Low display order per spec,
  // independent of each bucket's value.
  renderRiskBarChart() {
    if (!this.viewReady || !this.riskBarChartRef || this.isLoading) return;
    if (this.riskBarChartInstance) this.riskBarChartInstance.destroy();

    const r = this.riskDistribution;
    const cats = [
      { label: 'High Risk', value: r.high, color: CHART_PALETTE.red },
      { label: 'Medium Risk', value: r.medium, color: CHART_PALETTE.amber },
      { label: 'Low Risk', value: r.low, color: CHART_PALETTE.teal },
    ];
    const maxValue = Math.max(1, ...cats.map(c => c.value));

    // Draws each bar's own count just past its end — Chart.js's own
    // plugin API (a plain object with lifecycle hooks), registered only
    // on this chart's own `plugins` array below, not globally — no new
    // library, no effect on any other chart on this page.
    const valueLabelPlugin = {
      id: 'riskValueLabels',
      afterDatasetsDraw(chart: any) {
        const { ctx } = chart;
        const meta = chart.getDatasetMeta(0);
        meta.data.forEach((bar: any, i: number) => {
          const value = chart.data.datasets[0].data[i];
          ctx.save();
          ctx.fillStyle = '#E6E7EE';
          ctx.font = '600 10.5px Segoe UI, sans-serif';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(String(value), bar.x + 6, bar.y);
          ctx.restore();
        });
      },
    };

    const ctx = this.riskBarChartRef.nativeElement.getContext('2d');
    this.riskBarChartInstance = new Chart(ctx, {
      type: 'bar',
      plugins: [valueLabelPlugin],
      data: {
        labels: cats.map(c => c.label),
        datasets: [{
          data: cats.map(c => c.value),
          backgroundColor: cats.map(c => c.color),
          borderRadius: 3, borderSkipped: false,
          // Thinner bars with visible gaps between rows — Chart.js
          // defaults (0.8/0.9) read as thick, near-touching blocks for
          // a 3-row list like this.
          categoryPercentage: 0.6, barPercentage: 0.5,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        // Room on the right for the value label so it never gets
        // clipped at the canvas edge.
        layout: { padding: { right: 18 } },
        animation: this.chartLoadAnimation(),
        plugins: { legend: { display: false } },
        scales: {
          x: {
            display: true,
            beginAtZero: true,
            suggestedMax: Math.max(maxValue + 1, 4),
            ticks: { stepSize: 1, font: { size: 9 }, color: '#8A8D9E' },
            grid: { color: 'rgba(255, 255, 255, 0.08)' },
          },
          y: { ticks: { font: { size: 10.5 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  // Low/Medium/High transaction counts, reusing riskLevelFor() (the
  // SAME existing transaction risk classification already driving the
  // queue table's/Priority Queue's risk badges). Sole remaining
  // consumer is Audit Risk Distribution above (the earlier Transaction
  // Risk Distribution doughnut that also read this getter has been
  // replaced by Top Risk Suppliers, below, which duplicated the same
  // risk information in a second chart).
  get riskDistribution() {
    let low = 0, medium = 0, high = 0;
    for (const t of this.transactions) {
      const level = this.riskLevelFor(t);
      if (level === 'HIGH') high++;
      else if (level === 'MEDIUM') medium++;
      else low++;
    }
    return { low, medium, high };
  }

  // ── Top Risk Suppliers — replaces the old Transaction Risk
  // Distribution doughnut, whose Low/Medium/High breakdown duplicated
  // Audit Risk Distribution's own information. Groups the SAME
  // transactions array by supplier, scoring each transaction with
  // riskLevelFor() (shared with riskDistribution above — no second risk
  // calculation) plus the auditor's own review outcome, then adds each
  // supplier's authenticity failures from authenticityFailBySupplier
  // (built in loadAuthenticity() from the SAME /authenticity call
  // already made for Authenticity Outcomes — no new backend call). ──

  // High risk transaction: +3, Medium risk transaction: +2, Sent Back
  // review: +2, Need Review status: +1 — reads riskLevelFor()/
  // latest_review_action/workflow_status, the same fields Review
  // Workload and Audit Risk Distribution already read off this
  // transaction.
  private transactionRiskScore(t: any): number {
    let score = 0;
    const level = this.riskLevelFor(t);
    if (level === 'HIGH') score += 3;
    else if (level === 'MEDIUM') score += 2;
    if (t.latest_review_action === 'returned') score += 2;
    if (t.workflow_status === 'NEED REVIEW') score += 1;
    return score;
  }

  get topRiskSuppliers(): { supplier: string; score: number }[] {
    const scores = new Map<string, number>();
    for (const t of this.transactions) {
      const supplier = t.supplier || 'Unknown supplier';
      scores.set(supplier, (scores.get(supplier) || 0) + this.transactionRiskScore(t));
    }
    // Authenticity failure: +3 per failed check recorded for that supplier.
    for (const [supplier, failCount] of this.authenticityFailBySupplier) {
      scores.set(supplier, (scores.get(supplier) || 0) + failCount * 3);
    }
    return Array.from(scores.entries())
      .map(([supplier, score]) => ({ supplier, score }))
      .filter(s => s.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 5);
  }

  // Linear RGB interpolation between CHART_PALETTE's own red and violet
  // tokens (no new hex values) — t=0 (highest exposure, this list's top
  // supplier) reads red/pink, t=1 (lowest exposure among the displayed
  // suppliers) reads purple, per spec.
  private riskColor(t: number): string {
    const clamped = Math.max(0, Math.min(1, t));
    const from = this.hexToRgb(CHART_PALETTE.red);
    const to = this.hexToRgb(CHART_PALETTE.violet);
    const r = Math.round(from[0] + (to[0] - from[0]) * clamped);
    const g = Math.round(from[1] + (to[1] - from[1]) * clamped);
    const b = Math.round(from[2] + (to[2] - from[2]) * clamped);
    return `rgb(${r}, ${g}, ${b})`;
  }

  private hexToRgb(hex: string): [number, number, number] {
    return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
  }

  renderTopSuppliersChart() {
    if (!this.viewReady || !this.topSuppliersChartRef || this.isLoading || !this.authenticityLoaded) return;
    if (this.topSuppliersChartInstance) this.topSuppliersChartInstance.destroy();

    const suppliers = this.topRiskSuppliers;
    if (!suppliers.length) return; // canvas isn't in the DOM here (*ngIf) — the empty state shows instead

    const maxScore = suppliers[0].score;
    const minScore = suppliers[suppliers.length - 1].score;
    const range = maxScore - minScore || 1;
    const barColors = suppliers.map(s => this.riskColor((maxScore - s.score) / range));
    const maxValue = Math.max(1, ...suppliers.map(s => s.score));

    // Draws each bar's own score just past its end — same inline
    // Chart.js plugin technique Audit Risk Distribution already uses,
    // registered only on this chart's own `plugins` array below.
    const valueLabelPlugin = {
      id: 'topSuppliersValueLabels',
      afterDatasetsDraw(chart: any) {
        const { ctx } = chart;
        const meta = chart.getDatasetMeta(0);
        meta.data.forEach((bar: any, i: number) => {
          const value = chart.data.datasets[0].data[i];
          ctx.save();
          ctx.fillStyle = '#E6E7EE';
          ctx.font = '600 10.5px Segoe UI, sans-serif';
          ctx.textAlign = 'left';
          ctx.textBaseline = 'middle';
          ctx.fillText(String(value), bar.x + 6, bar.y);
          ctx.restore();
        });
      },
    };

    const ctx = this.topSuppliersChartRef.nativeElement.getContext('2d');
    this.topSuppliersChartInstance = new Chart(ctx, {
      type: 'bar',
      plugins: [valueLabelPlugin],
      data: {
        labels: suppliers.map(s => s.supplier),
        datasets: [{
          data: suppliers.map(s => s.score),
          backgroundColor: barColors,
          borderRadius: 3, borderSkipped: false,
          categoryPercentage: 0.6, barPercentage: 0.5,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { right: 18 } },
        animation: this.chartLoadAnimation(),
        plugins: { legend: { display: false } },
        scales: {
          x: {
            display: true,
            beginAtZero: true,
            suggestedMax: Math.max(maxValue + 1, 4),
            ticks: { stepSize: 1, font: { size: 9 }, color: '#8A8D9E' },
            grid: { color: 'rgba(255, 255, 255, 0.08)' },
          },
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
