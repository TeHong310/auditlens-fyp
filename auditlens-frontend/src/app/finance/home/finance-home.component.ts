import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { forkJoin, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

// Same required-action label lookup used in finance/corrections and
// finance/correction-detail — mirrors helpers/send_back.py's
// REQUIRED_ACTIONS exactly, kept as its own copy per this codebase's
// established per-component convention (no shared constants file).
const REQUIRED_ACTION_LABELS: Record<string, string> = {
  upload_missing_document: 'Upload missing document',
  correct_extracted_information: 'Correct extracted information',
  provide_written_explanation: 'Provide written explanation',
  confirm_duplicate_submission: 'Confirm duplicate submission',
  replace_incorrect_document: 'Replace incorrect document',
  verify_amount_or_quantity: 'Verify amount or quantity',
  confirm_supplier_information: 'Confirm supplier information',
  other: 'Other',
};

// Same palette as Auditor Home's dashboard (auditor-dashboard.component.
// ts) — kept as its own copy per this codebase's per-component
// convention, used only for chart decoration so the two "Home"
// dashboards read as one visual family.
const CHART_PALETTE = {
  violet: '#8B5CF6', blue: '#3B82F6', cyan: '#22D3EE', teal: '#2DD4BF',
  green: '#34D399', amber: '#FBBF24', orange: '#FB923C', coral: '#FB7185',
  red: '#F43F5E', pink: '#F472B6',
};

@Component({
  selector: 'app-finance-home',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './finance-home.component.html',
  styleUrls: ['./finance-home.component.css']
})
export class FinanceHomeComponent implements OnInit, AfterViewInit {
  @ViewChild('uploadTrendChart') uploadTrendChartRef!: ElementRef;
  @ViewChild('ocrPerfChart') ocrPerfChartRef!: ElementRef;
  @ViewChild('docTypeChart') docTypeChartRef!: ElementRef;
  @ViewChild('ocrConfidenceChart') ocrConfidenceChartRef!: ElementRef;
  @ViewChild('correctionChart') correctionChartRef!: ElementRef;

  documents: any[] = [];
  allDocuments: any[] = [];
  totalUploaded: number = 0;
  totalOcrProcessed: number = 0;
  totalUnderReview: number = 0;
  totalApproved: number = 0;
  totalReturned: number = 0;
  avgConfidence: number = 0;
  isLoading: boolean = false;

  // ── Pending Finance Action card — missingDocsCount is the subset of
  // this month's returned invoices whose latest send_back_cycle
  // requires uploading a missing document (see loadCyclesForActionStats
  // below); totalReturned above already IS "unresolved correction
  // cases" (a document stays 'returned' — not 'resubmitted' — for
  // exactly as long as its cycle is unresolved). ──
  missingDocsCount: number = 0;

  // ── Document Processing Queue's Required Action column + the two
  // action stats above both read from the SAME send_back_cycles data
  // Correction Center already reads via GET /reviews/send-back-cycles/
  // <id> — keyed by document_id, populated for EVERY returned document
  // this month (not just the rows shown in the queue), via
  // loadCyclesForActionStats() below. ──
  latestCycleByDocId: { [documentId: number]: any } = {};

  // ── Document Type Distribution + Correction Analysis both need to
  // know, per invoice, whether a PO/GR exists — reuses the EXISTING
  // GET /documents/po/list and GET /documents/gr/list endpoints
  // (already finance-scoped, already used elsewhere in this app, e.g.
  // finance-transaction-create), fetched once here in parallel with
  // loadDocuments(). Each PO/GR row's own document_id column already
  // points back at the INVOICE it belongs to (confirmed against
  // routes/documents.py's upload-po/upload-gr handlers and the
  // existing /ocr-review/invoice/<id>/related-docs endpoint, which
  // queries purchase_orders/goods_receipts the same way) — so simple
  // Set membership tells us, per invoice, whether its PO/GR exists. No
  // new backend endpoint, no transaction-package logic touched. ──
  poList: any[] = [];
  grList: any[] = [];
  poDocumentIds: Set<number> = new Set();
  grDocumentIds: Set<number> = new Set();
  poGrLoaded: boolean = false;
  cyclesLoaded: boolean = false;

  // ── Derived, presentation-only breakdowns for the 6-chart grid —
  // each computed purely from data already fetched above, no new
  // business logic. ──
  docStatusOverview = { processed: 0, pendingReview: 0, returned: 0 };
  documentTypeDistribution = { invoice: 0, po: 0, gr: 0 };
  correctionAnalysis: { label: string; value: number }[] = [];
  priorityFinanceItems: any[] = [];

  private thisMonthDocsCache: any[] = [];
  private viewReady = false;
  private uploadTrendChartInstance: any = null;
  private ocrPerfChartInstance: any = null;
  private docTypeChartInstance: any = null;
  private ocrConfidenceChartInstance: any = null;
  private correctionChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    // Both fired together, in parallel — loadPoGrLists() is not
    // chained behind loadDocuments(), avoiding a slower waterfall.
    this.loadDocuments();
    this.loadPoGrLists();
  }

  ngAfterViewInit() {
    this.viewReady = true;
    // Any chart whose data already resolved before the view was ready
    // gets drawn now; charts still waiting on data draw themselves
    // later, from their own load-completion callback below.
    this.renderUploadTrendChart();
    this.renderOcrPerfChart();
    this.renderDocTypeChart();
    this.renderOcrConfidenceChart();
    this.renderCorrectionChart();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadDocuments() {
    this.isLoading = true;
    this.http.get<any>(`${this.apiUrl}/documents/`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.allDocuments = res.documents;

        // Current month filter
        const now = new Date();
        const currentMonth = now.getMonth();
        const currentYear = now.getFullYear();

        const thisMonthDocs = res.documents.filter((d: any) => {
          const date = new Date(d.uploaded_at);
          return date.getMonth() === currentMonth &&
            date.getFullYear() === currentYear;
        });
        this.thisMonthDocsCache = thisMonthDocs;

        // Stats Cards — current month only
        this.totalUploaded = thisMonthDocs.length;
        this.totalOcrProcessed = thisMonthDocs.filter((d: any) =>
          ['ocr_done', 'under_review', 'approved', 'returned', 'resubmitted'].includes(d.status)
        ).length;
        this.totalUnderReview = thisMonthDocs.filter((d: any) =>
          d.status === 'under_review'
        ).length;
        this.totalApproved = thisMonthDocs.filter((d: any) =>
          d.status === 'approved'
        ).length;
        this.totalReturned = thisMonthDocs.filter((d: any) =>
          d.status === 'returned'
        ).length;

        // Avg confidence — current month only
        const withConfidence = thisMonthDocs.filter((d: any) => d.ocr_confidence);
        if (withConfidence.length > 0) {
          const sum = withConfidence.reduce((acc: number, d: any) =>
            acc + parseFloat(d.ocr_confidence), 0);
          this.avgConfidence = Math.round(sum / withConfidence.length);
        }

        // Document Processing Queue — current month only, last 5
        this.documents = thisMonthDocs.slice(0, 5);

        this.computeDocStatusOverview(thisMonthDocs);
        this.computePriorityFinanceItems(thisMonthDocs);

        this.isLoading = false;
        this.cdr.detectChanges();
        this.renderUploadTrendChart();
        this.renderOcrPerfChart();
        this.renderOcrConfidenceChart();

        const returnedThisMonth = thisMonthDocs.filter((d: any) => d.status === 'returned');
        this.loadCyclesForActionStats(returnedThisMonth);
      },
      error: () => { this.isLoading = false; }
    });
  }

  // Reuses the EXISTING GET /documents/po/list and GET /documents/gr/
  // list endpoints (already finance-scoped server-side) — feeds both
  // Document Type Distribution and (together with the send-back-cycles
  // data below) Correction Analysis. No new backend endpoint.
  loadPoGrLists() {
    forkJoin({
      po: this.http.get<any>(`${this.apiUrl}/documents/po/list`, { headers: this.getHeaders() })
        .pipe(catchError(() => of({ purchase_orders: [] }))),
      gr: this.http.get<any>(`${this.apiUrl}/documents/gr/list`, { headers: this.getHeaders() })
        .pipe(catchError(() => of({ goods_receipts: [] }))),
    }).subscribe(({ po, gr }) => {
      this.poList = po.purchase_orders || [];
      this.grList = gr.goods_receipts || [];
      this.poDocumentIds = new Set(this.poList.map((p: any) => p.document_id));
      this.grDocumentIds = new Set(this.grList.map((g: any) => g.document_id));
      this.poGrLoaded = true;

      this.computeDocumentTypeDistribution();
      this.computeCorrectionAnalysis();
      this.cdr.detectChanges();
      this.renderDocTypeChart();
      this.renderCorrectionChart();
    });
  }

  // Reuses GET /reviews/send-back-cycles/<id> (already used identically
  // by finance/corrections and finance-ocr-review) for EVERY returned
  // document this month — not just the rows shown in the queue — so
  // the Pending Finance Action card, Correction Analysis, and Priority
  // Finance Action panel all reflect the dashboard's true scope (this
  // month). No new backend endpoint; the request volume is bounded by
  // how many invoices are actually returned this month (typically
  // small), the same population the Correction Required stat counts.
  private loadCyclesForActionStats(returnedDocs: any[]) {
    const returnedIds = returnedDocs.map(d => d.document_id);
    if (!returnedIds.length) {
      this.missingDocsCount = 0;
      this.cyclesLoaded = true;
      this.computeCorrectionAnalysis();
      this.renderCorrectionChart();
      return;
    }

    const requests: { [documentId: number]: any } = {};
    for (const id of returnedIds) {
      requests[id] = this.http.get<any>(`${this.apiUrl}/reviews/send-back-cycles/${id}`, {
        headers: this.getHeaders()
      }).pipe(catchError(() => of(null)));
    }

    forkJoin(requests).subscribe((results: any) => {
      let missingCount = 0;
      for (const id of returnedIds) {
        const cycles = results[id]?.cycles || [];
        const latest = cycles.length ? cycles[cycles.length - 1] : null;
        this.latestCycleByDocId[id] = latest;
        if (latest?.required_actions?.includes('upload_missing_document')) missingCount++;
      }
      this.missingDocsCount = missingCount;
      this.cyclesLoaded = true;

      this.computeCorrectionAnalysis();
      this.computePriorityFinanceItems(this.thisMonthDocsCache);
      this.cdr.detectChanges();
      this.renderCorrectionChart();
    });
  }

  // Chart 3 — Document Status Overview: an exhaustive-as-possible 3-way
  // split of this month's documents. Documents still mid-OCR
  // (ocr_processing) intentionally aren't counted in any bucket, so the
  // 3 percentages describe outcomes reached so far, not literally every
  // upload this month.
  private computeDocStatusOverview(thisMonthDocs: any[]) {
    let processed = 0, pendingReview = 0, returned = 0;
    for (const d of thisMonthDocs) {
      if (d.status === 'approved') processed++;
      else if (d.status === 'returned') returned++;
      else if (['under_review', 'resubmitted', 'ocr_done'].includes(d.status)) pendingReview++;
    }
    this.docStatusOverview = { processed, pendingReview, returned };
  }

  // Chart 4 — Document Type Distribution: Invoice count from
  // allDocuments (already fetched), PO/GR counts from poList/grList
  // (loadPoGrLists above) — all filtered to this month for consistency
  // with the KPI cards and the rest of the secondary chart row.
  private computeDocumentTypeDistribution() {
    const now = new Date();
    const inMonth = (dateStr: string) => {
      const d = new Date(dateStr);
      return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear();
    };
    const invoiceCount = this.allDocuments.filter((d: any) => inMonth(d.uploaded_at)).length;
    const poCount = this.poList.filter((p: any) => inMonth(p.uploaded_at)).length;
    const grCount = this.grList.filter((g: any) => inMonth(g.uploaded_at)).length;
    this.documentTypeDistribution = { invoice: invoiceCount, po: poCount, gr: grCount };
  }

  // Chart 6 — Correction Analysis: 5 factors observed across this
  // month's returned invoices. Missing PO / Missing GR / Low OCR are
  // measured conditions (PO/GR presence, OCR confidence < 75) rather
  // than a single stated reason, since a return can have more than one
  // contributing factor; Invalid Amount / Duplicate come from the
  // auditor's actual stated return_reason_category on each invoice's
  // latest send-back cycle. Needs both loadPoGrLists() and
  // loadCyclesForActionStats() to have resolved.
  private computeCorrectionAnalysis() {
    if (!this.poGrLoaded || !this.cyclesLoaded) return;

    const now = new Date();
    const returned = this.allDocuments.filter((d: any) => {
      if (d.status !== 'returned') return false;
      const date = new Date(d.uploaded_at);
      return date.getMonth() === now.getMonth() && date.getFullYear() === now.getFullYear();
    });

    let missingPO = 0, missingGR = 0, lowOcr = 0, invalidAmount = 0, duplicate = 0;
    for (const doc of returned) {
      if (!this.poDocumentIds.has(doc.document_id)) missingPO++;
      if (!this.grDocumentIds.has(doc.document_id)) missingGR++;
      if (doc.ocr_confidence && parseFloat(doc.ocr_confidence) < 75) lowOcr++;
      const category = this.latestCycleByDocId[doc.document_id]?.return_reason_category;
      if (category === 'amount_or_quantity_requires_verification') invalidAmount++;
      if (category === 'possible_duplicate_invoice') duplicate++;
    }

    this.correctionAnalysis = [
      { label: 'Missing PO', value: missingPO },
      { label: 'Missing GR', value: missingGR },
      { label: 'Low OCR', value: lowOcr },
      { label: 'Invalid Amount', value: invalidAmount },
      { label: 'Duplicate', value: duplicate },
    ];
  }

  // Priority Finance Action panel — this month's returned invoices,
  // ranked by the auditor-set priority on their latest send-back cycle
  // (urgent > high > normal), then by age (oldest unresolved first).
  // Mirrors Auditor Home's Priority Review Queue computation.
  private computePriorityFinanceItems(thisMonthDocs: any[]) {
    const returned = thisMonthDocs.filter((d: any) => d.status === 'returned');
    returned.sort((a: any, b: any) => {
      const rankDiff = this.priorityRank(b) - this.priorityRank(a);
      if (rankDiff !== 0) return rankDiff;
      return new Date(a.uploaded_at || 0).getTime() - new Date(b.uploaded_at || 0).getTime();
    });
    this.priorityFinanceItems = returned.slice(0, 4);
  }

  private priorityRank(doc: any): number {
    const p = this.latestCycleByDocId[doc.document_id]?.priority || 'normal';
    return p === 'urgent' ? 2 : p === 'high' ? 1 : 0;
  }

  priorityLabel(doc: any): string {
    const p = this.latestCycleByDocId[doc.document_id]?.priority || 'normal';
    return p.toUpperCase();
  }

  priorityBadgeClass(doc: any): string {
    const p = this.latestCycleByDocId[doc.document_id]?.priority || 'normal';
    return (p === 'urgent' || p === 'high') ? 'badge-returned' : 'badge-pending';
  }

  pct(n: number): string {
    return this.totalUploaded > 0 ? ((n / this.totalUploaded) * 100).toFixed(1) : '0';
  }

  // Status Breakdown-style mini radial ring — pure CSS conic-gradient,
  // same technique as Auditor Home's Status Breakdown card.
  ringGradient(value: number, total: number, color: string): string {
    const percent = total > 0 ? (value / total) * 100 : 0;
    return `conic-gradient(${color} 0% ${percent}%, var(--bg-hover) ${percent}% 100%)`;
  }

  renderUploadTrendChart() {
    if (!this.viewReady || !this.uploadTrendChartRef) return;
    if (this.uploadTrendChartInstance) this.uploadTrendChartInstance.destroy();

    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const ocrProcessedStatuses = ['ocr_done', 'under_review', 'approved', 'returned', 'resubmitted'];

    const uploadCounts: { [key: string]: number } = {};
    const ocrCounts: { [key: string]: number } = {};
    this.allDocuments.forEach(doc => {
      const key = months[new Date(doc.uploaded_at).getMonth()];
      uploadCounts[key] = (uploadCounts[key] || 0) + 1;
      if (ocrProcessedStatuses.includes(doc.status)) {
        ocrCounts[key] = (ocrCounts[key] || 0) + 1;
      }
    });

    const uploadData = months.map(m => uploadCounts[m] || 0);
    const ocrData = months.map(m => ocrCounts[m] || 0);

    const ctx = this.uploadTrendChartRef.nativeElement.getContext('2d');
    const barGradient = ctx.createLinearGradient(0, 0, 0, 200);
    barGradient.addColorStop(0, CHART_PALETTE.blue);
    barGradient.addColorStop(1, CHART_PALETTE.cyan);

    this.uploadTrendChartInstance = new Chart(ctx, {
      data: {
        labels: months,
        datasets: [
          {
            type: 'bar' as const,
            label: 'Uploaded',
            data: uploadData,
            backgroundColor: barGradient,
            borderRadius: 5,
            borderSkipped: false,
            yAxisID: 'y',
          },
          {
            type: 'line' as const,
            label: 'OCR Processed',
            data: ocrData,
            borderColor: CHART_PALETTE.teal,
            backgroundColor: 'rgba(45, 212, 191, 0.12)',
            borderWidth: 2,
            pointBackgroundColor: CHART_PALETTE.teal,
            pointRadius: 3,
            tension: 0.4,
            fill: true,
            yAxisID: 'y',
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, position: 'top' as const, labels: { boxWidth: 8, font: { size: 10.5 }, padding: 8 } },
          tooltip: { mode: 'index' as const, intersect: false }
        },
        scales: {
          y: { beginAtZero: true, ticks: { stepSize: 1, font: { size: 9.5 } }, grid: { color: 'rgba(255,255,255,0.06)' } },
          x: { ticks: { font: { size: 9.5 } }, grid: { display: false } }
        }
      }
    });
  }

  renderOcrPerfChart() {
    if (!this.viewReady || !this.ocrPerfChartRef) return;
    if (this.ocrPerfChartInstance) this.ocrPerfChartInstance.destroy();

    const now = new Date();
    const thisMonthDocs = this.allDocuments.filter(d => {
      const date = new Date(d.uploaded_at);
      return date.getMonth() === now.getMonth() && date.getFullYear() === now.getFullYear();
    });
    const last10 = thisMonthDocs.filter(d => d.ocr_confidence).slice(-10);
    const labels = last10.map((_, i) => `Doc ${i + 1}`);
    const data = last10.map(d => parseFloat(d.ocr_confidence));

    const ctx = this.ocrPerfChartRef.nativeElement.getContext('2d');
    this.ocrPerfChartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'OCR Confidence %',
          data,
          borderColor: CHART_PALETTE.violet,
          backgroundColor: 'rgba(139, 92, 246, 0.12)',
          borderWidth: 2,
          pointBackgroundColor: CHART_PALETTE.violet,
          pointRadius: 3,
          tension: 0.4,
          fill: true,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { min: 0, max: 100, ticks: { stepSize: 20, font: { size: 9.5 } }, grid: { color: 'rgba(255,255,255,0.06)' } },
          x: { ticks: { font: { size: 9.5 } }, grid: { display: false } }
        }
      }
    });
  }

  renderDocTypeChart() {
    if (!this.viewReady || !this.docTypeChartRef || !this.poGrLoaded) return;
    if (this.docTypeChartInstance) this.docTypeChartInstance.destroy();

    const t = this.documentTypeDistribution;
    const ctx = this.docTypeChartRef.nativeElement.getContext('2d');
    this.docTypeChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: ['Invoice', 'Purchase Order', 'Goods Receipt'],
        datasets: [{
          data: [t.invoice, t.po, t.gr],
          backgroundColor: [CHART_PALETTE.blue, CHART_PALETTE.violet, CHART_PALETTE.cyan],
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

  renderOcrConfidenceChart() {
    if (!this.viewReady || !this.ocrConfidenceChartRef) return;
    if (this.ocrConfidenceChartInstance) this.ocrConfidenceChartInstance.destroy();

    const now = new Date();
    const thisMonthDocs = this.allDocuments.filter(d => {
      const date = new Date(d.uploaded_at);
      return date.getMonth() === now.getMonth() && date.getFullYear() === now.getFullYear() && d.ocr_confidence;
    });

    const buckets = [
      { label: '90-100', min: 90, max: 101, color: CHART_PALETTE.green },
      { label: '80-89', min: 80, max: 90, color: CHART_PALETTE.teal },
      { label: '70-79', min: 70, max: 80, color: CHART_PALETTE.amber },
      { label: '60-69', min: 60, max: 70, color: CHART_PALETTE.orange },
      { label: '<60', min: 0, max: 60, color: CHART_PALETTE.red },
    ];
    const counts = buckets.map(b => thisMonthDocs.filter(d => {
      const c = parseFloat(d.ocr_confidence);
      return c >= b.min && c < b.max;
    }).length);

    const ctx = this.ocrConfidenceChartRef.nativeElement.getContext('2d');
    this.ocrConfidenceChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: buckets.map(b => b.label),
        datasets: [{
          data: counts,
          backgroundColor: buckets.map(b => b.color),
          borderRadius: 4, borderSkipped: false,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { display: false, beginAtZero: true, ticks: { stepSize: 1 } },
          x: { ticks: { font: { size: 9 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  renderCorrectionChart() {
    if (!this.viewReady || !this.correctionChartRef || !this.poGrLoaded || !this.cyclesLoaded) return;
    if (this.correctionChartInstance) this.correctionChartInstance.destroy();

    const cats = this.correctionAnalysis;
    const categoryColors = [CHART_PALETTE.coral, CHART_PALETTE.pink, CHART_PALETTE.amber, CHART_PALETTE.orange, CHART_PALETTE.violet];
    const ctx = this.correctionChartRef.nativeElement.getContext('2d');
    this.correctionChartInstance = new Chart(ctx, {
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
          y: { ticks: { font: { size: 9.5 }, color: '#E6E7EE' }, grid: { display: false } }
        }
      }
    });
  }

  goToUpload() { this.router.navigate(['/finance/upload']); }
  goToOcrReview() { this.router.navigate(['/finance/ocr-review']); }
  goToCorrections() { this.router.navigate(['/finance/corrections']); }

  getStatusClass(status: string): string {
    switch (status) {
      case 'ocr_done': return 'badge-processed';
      case 'under_review': return 'badge-review';
      case 'approved': return 'badge-matched';
      case 'returned': return 'badge-returned';
      default: return 'badge-pending';
    }
  }

  getStatusLabel(status: string): string {
    switch (status) {
      case 'ocr_done': return 'OCR Done';
      case 'under_review': return 'Under Review';
      case 'approved': return 'Approved';
      case 'returned': return 'Returned';
      case 'resubmitted': return 'Resubmitted';
      case 'ocr_processing': return 'Processing...';
      default: return status;
    }
  }

  ocrConfidenceLabel(doc: any): string {
    return doc.ocr_confidence ? `${Math.round(parseFloat(doc.ocr_confidence))}%` : '-';
  }

  ageDays(dateStr: string): number {
    if (!dateStr) return 0;
    return Math.max(0, Math.floor((Date.now() - new Date(dateStr).getTime()) / 86400000));
  }

  // ── Document Processing Queue: Required Action column — only
  // populated for 'returned' rows, from the SAME send_back_cycles data
  // Correction Center already shows (see loadCyclesForActionStats
  // above). Every other status has nothing outstanding to require, so
  // it's shown as a plain dash rather than an invented action. ──

  requiredActionLabel(doc: any): string {
    if (doc.status !== 'returned') return '-';
    const cycle = this.latestCycleByDocId[doc.document_id];
    const actions = cycle?.required_actions || [];
    if (!actions.length) return 'Awaiting Finance correction';
    return actions.map((a: string) => REQUIRED_ACTION_LABELS[a] || a).join(', ');
  }

  // ── Document Processing Queue: dynamic Action column — label + click
  // handler per status. Only genuinely actionable statuses ('returned'
  // and the still-in-flight ocr_done/ocr_processing default) render as
  // a clickable button; 'under_review'/'resubmitted' and 'approved' are
  // waiting-on-someone-else or already-done states, so they render as
  // a plain status label instead of a button (see isActionClickable
  // below). Reuses existing routes/endpoints only: Correction Center
  // (already built) and the existing OCR Review page — no new backend
  // API of any kind. ──

  actionLabel(doc: any): string {
    switch (doc.status) {
      case 'returned': return 'Fix Issue';
      case 'under_review': return 'Waiting';
      case 'resubmitted': return 'Waiting';
      case 'approved': return 'No Action';
      default: return 'Review'; // ocr_done / ocr_processing / any other in-flight state
    }
  }

  isActionClickable(doc: any): boolean {
    return doc.status === 'returned' || (doc.status !== 'under_review' && doc.status !== 'resubmitted' && doc.status !== 'approved');
  }

  onAction(doc: any) {
    switch (doc.status) {
      case 'returned':
        this.router.navigate(['/finance/corrections/detail'], { queryParams: { document_id: doc.document_id } });
        return;
      case 'under_review':
      case 'resubmitted':
      case 'approved':
        return; // no action — see isActionClickable above, these never render a clickable button
      default:
        // ocr_done / ocr_processing / any other in-flight state — the
        // OCR Review page is exactly where these still-actionable
        // documents already live and can be edited/submitted.
        this.router.navigate(['/finance/ocr-review']);
        return;
    }
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }
}
