import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { forkJoin, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';
import { FinanceUserMenuComponent } from '../shared/finance-user-menu.component';
import { toMalaysiaDateKey } from '../../shared/datetime.util';

Chart.register(...registerables);

// Semantic status colours — single source of truth for this page's KPI
// accents / chart series / table badges / legends. Deliberately exact
// literals (not the app's global --accent/--success/--warning/--danger
// theme tokens, which are close but not identical) per this redesign's
// colour spec.
const COLOR_BRAND    = '#7C5CFC'; // Transaction Packages / Uploaded
const COLOR_APPROVED = '#55D6A9'; // Approved / Full Match
const COLOR_RETURNED = '#FF667A'; // Returned / Mismatch / Missing Documents
const COLOR_REVIEW   = '#FFB84D'; // Under Review / Review Required
const COLOR_OCR       = '#4DA3FF'; // Average OCR Confidence
const COLOR_PENDING  = '#8B95A7'; // Pending
// Top Vendors — blue shades only, never red/green/orange (those are
// reserved for status meaning above).
const VENDOR_SHADES = ['#2E6DA4', '#3E8ED0', '#4DA3FF', '#6FB6FF', '#9CCBFF', '#8B95A7'];

// A package or standalone-invoice's status is derived from the highest-
// priority member document — same ranking finance-home.component.ts's
// computeQueueGroups() already uses to pick each group's "primary" doc
// (vendor display, etc.), reused here unchanged.
const STATUS_PRIORITY: Record<string, number> = {
  returned: 5, under_review: 4, resubmitted: 4, ocr_processing: 3, ocr_done: 2, approved: 1,
};

@Component({
  selector: 'app-finance-report',
  standalone: true,
  imports: [CommonModule, FinanceUserMenuComponent],
  templateUrl: './finance-report.component.html',
  styleUrls: ['./finance-report.component.css']
})
export class FinanceReportComponent implements OnInit, AfterViewInit {
  @ViewChild('donutChart') donutChartRef!: ElementRef;
  @ViewChild('vendorChart') vendorChartRef!: ElementRef;
  @ViewChild('trendChart') trendChartRef!: ElementRef;

  // One row per transaction package (or standalone invoice) — the
  // table/KPI/chart-facing array. Built by computeGroupedRows() below
  // once BOTH the per-invoice report and the package grouping data have
  // loaded.
  documents: any[] = [];

  // Deduped per-invoice records (one per document_id, latest review
  // record wins) from GET /reviews/finance-report — the input to
  // grouping, and still the source for invoice-level figures that
  // don't make sense re-averaged per package (Average OCR Confidence).
  private invoiceRecords: any[] = [];
  private reportLoaded: boolean = false;

  // Package grouping — reuses the EXACT SAME data source and shape
  // Finance Home's own Document Processing Queue groups by (GET
  // /transaction-packages + GET /transaction-packages/<id>, the same
  // Finance-scoped endpoints finance-home.component.ts::
  // loadTransactionPackages() already calls) — not document_id, vendor
  // name, or guessed PO numbers. No new endpoint, no new grouping
  // logic: this is the same map shape and the same forkJoin-per-package
  // fetch, copied from finance-home.component.ts.
  private packageGroupByDocId: Map<number, { packageId: number; invoiceNumbers: string[]; poNumbers: string[]; grNumbers: string[] }> = new Map();
  private packagesLoaded: boolean = false;

  isLoading: boolean = false;
  chartReady: boolean = false;

  // KPIs — all counted over grouped rows (this.documents), per "count
  // grouped packages, not individual invoices".
  totalPackages: number = 0;
  totalApproved: number = 0;
  totalReturned: number = 0;
  totalUnderReview: number = 0;
  avgOcrConfidence: number = 0;

  readonly colorBrand = COLOR_BRAND;
  readonly colorApproved = COLOR_APPROVED;
  readonly colorReturned = COLOR_RETURNED;
  readonly colorReview = COLOR_REVIEW;
  readonly colorOcr = COLOR_OCR;

  private donutChartInstance: any = null;
  private vendorChartInstance: any = null;
  private trendChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    // Fired together, in parallel — neither is chained behind the
    // other; whichever resolves last is the one that actually produces
    // the grouped rows (see computeGroupedRows()'s own gate), same
    // pattern finance-home.component.ts uses for its own multi-source
    // load.
    this.loadReport();
    this.loadTransactionPackages();
  }

  ngAfterViewInit() {
    if (this.chartReady) this.renderAllCharts();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadReport() {
    this.isLoading = true;
    this.http.get<any>(`${this.apiUrl}/reviews/finance-report`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.invoiceRecords = this.dedupeByDocument(res.documents);
        this.reportLoaded = true;
        this.computeGroupedRows();
      },
      error: () => { this.isLoading = false; }
    });
  }

  // Keeps the row with the most recent reviewed_at (its Latest Remark)
  // for each document_id — GET /reviews/finance-report LEFT JOINs
  // review_records, so a document reviewed more than once (sent back,
  // then later approved) comes back as multiple rows; left un-deduped,
  // grouping/KPI counts would double/triple-count that one document.
  private dedupeByDocument(docs: any[]): any[] {
    const byId = new Map<number, any>();
    for (const doc of docs) {
      const existing = byId.get(doc.document_id);
      if (!existing || (doc.reviewed_at && (!existing.reviewed_at || doc.reviewed_at > existing.reviewed_at))) {
        byId.set(doc.document_id, doc);
      }
    }
    return Array.from(byId.values());
  }

  // Package grouping data source — copied from finance-home.component.
  // ts::loadTransactionPackages() unchanged (same two endpoints, same
  // request shape, same packageGroupByDocId map shape) so grouping here
  // can never disagree with what Finance Home already shows for the
  // same packages.
  loadTransactionPackages() {
    this.http.get<any[]>(`${this.apiUrl}/transaction-packages`, { headers: this.getHeaders() })
      .pipe(catchError(() => of([])))
      .subscribe((packages: any[]) => {
        if (!packages.length) {
          this.packagesLoaded = true;
          this.computeGroupedRows();
          return;
        }

        const requests: { [id: number]: any } = {};
        for (const pkg of packages) {
          requests[pkg.id] = this.http.get<any>(`${this.apiUrl}/transaction-packages/${pkg.id}`, { headers: this.getHeaders() })
            .pipe(catchError(() => of(null)));
        }

        forkJoin(requests).subscribe((results: any) => {
          const map = new Map<number, { packageId: number; invoiceNumbers: string[]; poNumbers: string[]; grNumbers: string[] }>();
          for (const pkg of packages) {
            const detail = results[pkg.id];
            const docs = detail?.documents;
            if (!docs) continue;

            const group = {
              packageId: pkg.id,
              invoiceNumbers: Array.from(new Set<string>(docs.invoices.map((d: any) => d.invoice_number).filter(Boolean))),
              poNumbers: Array.from(new Set<string>(docs.purchase_orders.map((p: any) => p.po_number).filter(Boolean))),
              grNumbers: Array.from(new Set<string>(docs.goods_receipts.map((g: any) => g.gr_number).filter(Boolean))),
            };
            for (const inv of docs.invoices) {
              map.set(inv.document_id, group);
            }
          }
          this.packageGroupByDocId = map;
          this.packagesLoaded = true;
          this.computeGroupedRows();
        });
      });
  }

  // Groups invoiceRecords by real transaction_package_id (via
  // packageGroupByDocId) into one row per package; an invoice not
  // linked into any package stays its own row, exactly like Finance
  // Home's own standalone rows. Needs both loads done.
  private computeGroupedRows() {
    if (!this.reportLoaded || !this.packagesLoaded) return;

    const docsByPackageId = new Map<number, any[]>();
    const standaloneDocs: any[] = [];
    for (const doc of this.invoiceRecords) {
      const group = this.packageGroupByDocId.get(doc.document_id);
      if (group) {
        const arr = docsByPackageId.get(group.packageId) || [];
        arr.push(doc);
        docsByPackageId.set(group.packageId, arr);
      } else {
        standaloneDocs.push(doc);
      }
    }

    const rows: any[] = [];
    for (const docs of docsByPackageId.values()) {
      const group = this.packageGroupByDocId.get(docs[0].document_id)!;
      rows.push(this.buildRow(group.packageId, docs, group.poNumbers, group.grNumbers));
    }
    for (const doc of standaloneDocs) {
      const poNumbers = doc.purchase_order_number ? [doc.purchase_order_number] : [];
      const grNumbers = doc.goods_receipt_number ? [doc.goods_receipt_number] : [];
      rows.push(this.buildRow(null, [doc], poNumbers, grNumbers));
    }

    rows.sort((a, b) => new Date(b.uploadedAt || 0).getTime() - new Date(a.uploadedAt || 0).getTime());
    this.documents = rows;

    this.computeStats();
    this.isLoading = false;
    this.chartReady = true;
    this.cdr.detectChanges();
    setTimeout(() => this.renderAllCharts(), 200);
  }

  // One package's (or one standalone invoice's) aggregate row.
  // poNumbers/grNumbers are already deduped by the caller (package:
  // from packageGroupByDocId, itself deduped via Set at load time;
  // standalone: a single-element array).
  private buildRow(packageId: number | null, docs: any[], poNumbers: string[], grNumbers: string[]): any {
    const primary = [...docs].sort((a, b) => (STATUS_PRIORITY[b.status] || 0) - (STATUS_PRIORITY[a.status] || 0))[0];
    const newest = [...docs].sort((a, b) => new Date(b.uploaded_at || 0).getTime() - new Date(a.uploaded_at || 0).getTime())[0];
    const latestReviewed = docs
      .filter(d => d.reviewed_at)
      .sort((a, b) => (b.reviewed_at || '').localeCompare(a.reviewed_at || ''))[0] || null;

    const invoiceNumbers = Array.from(new Set(docs.map(d => d.invoice_number).filter(Boolean)));
    const withAmount = docs.filter(d => d.total_amount != null);
    const totalAmount = withAmount.length ? withAmount.reduce((sum, d) => sum + Number(d.total_amount), 0) : null;
    const currency = docs.find(d => d.currency)?.currency || null;

    // Audit Status — Returned if ANY member is currently returned for
    // correction; Under Review if any active member is under review;
    // Approved only when the COMPLETE package is approved; otherwise
    // Pending.
    let auditStatusLabel: string;
    if (docs.some(d => d.status === 'returned')) auditStatusLabel = 'Returned';
    else if (docs.some(d => d.status === 'under_review')) auditStatusLabel = 'Under Review';
    else if (docs.every(d => d.status === 'approved')) auditStatusLabel = 'Approved';
    else auditStatusLabel = 'Pending';

    // Match Status — required PO/GR genuinely missing at the PACKAGE
    // level (poNumbers/grNumbers empty — a shared PO/GR covering
    // multiple invoices in the package still counts as present) decides
    // "Missing Documents" first; otherwise the worst-first cascade over
    // each member's own real matching result (overall_status, from the
    // same build_comparison()-backed value GET /reviews/finance-report
    // already returns per invoice — no new scoring formula).
    let matchStatusLabel: string;
    if (poNumbers.length === 0 || grNumbers.length === 0) {
      matchStatusLabel = 'Missing Documents';
    } else if (docs.some(d => d.overall_status === 'FAIL')) {
      matchStatusLabel = 'Mismatch';
    } else if (docs.some(d => d.overall_status === 'REVIEW' || d.overall_status === 'PARTIAL')) {
      matchStatusLabel = 'Review Required';
    } else if (docs.every(d => d.overall_status === 'PASS')) {
      matchStatusLabel = 'Full Match';
    } else {
      matchStatusLabel = 'Pending';
    }

    return {
      packageId,
      documentIds: docs.map(d => d.document_id),
      invoiceLabel: invoiceNumbers.length ? invoiceNumbers.join(', ') : (docs[0].file_name || '-'),
      relatedDocumentsLabel: this.formatRelatedDocuments(poNumbers, grNumbers),
      vendorName: primary.vendor_name || '-',
      totalAmount,
      currency,
      uploadedAt: newest.uploaded_at,
      matchStatusLabel,
      matchStatusClass: this.matchStatusClassFor(matchStatusLabel),
      auditStatusLabel,
      auditStatusClass: this.auditStatusClassFor(auditStatusLabel),
      latestRemark: latestReviewed?.comments || null,
      latestReviewedAt: latestReviewed?.reviewed_at || null,
    };
  }

  private formatRelatedDocuments(poNumbers: string[], grNumbers: string[]): string {
    const po = poNumbers.length ? `PO: ${poNumbers.join(', ')}` : 'PO: Not Uploaded';
    const gr = grNumbers.length ? `GR: ${grNumbers.join(', ')}` : 'GR: Not Uploaded';
    return `${po} · ${gr}`;
  }

  private matchStatusClassFor(label: string): string {
    if (label === 'Full Match') return 'badge-approved';
    if (label === 'Review Required') return 'badge-review';
    if (label === 'Mismatch' || label === 'Missing Documents') return 'badge-returned';
    return 'badge-pending'; // Pending
  }

  private auditStatusClassFor(label: string): string {
    if (label === 'Approved') return 'badge-approved';
    if (label === 'Returned') return 'badge-returned';
    if (label === 'Under Review') return 'badge-review';
    return 'badge-pending'; // Pending
  }

  private computeStats() {
    this.totalPackages = this.documents.length;
    this.totalApproved = this.documents.filter((d: any) => d.auditStatusLabel === 'Approved').length;
    this.totalReturned = this.documents.filter((d: any) => d.auditStatusLabel === 'Returned').length;
    this.totalUnderReview = this.documents.filter((d: any) => d.auditStatusLabel === 'Under Review').length;

    // Average OCR Confidence stays per INVOICE (an OCR score belongs to
    // one scanned document, not a package) — averaged across
    // invoiceRecords directly rather than averaging package-level
    // averages, which would over-weight packages with fewer invoices.
    const withOcr = this.invoiceRecords.filter((d: any) => d.ocr_confidence != null);
    if (withOcr.length > 0) {
      const sum = withOcr.reduce((acc: number, d: any) => acc + parseFloat(d.ocr_confidence), 0);
      this.avgOcrConfidence = Math.round(sum / withOcr.length);
    }
  }

  // Search bar removed — this page's table shows every loaded document,
  // unfiltered. Kept as a getter (rather than inlining this.documents at
  // each call site) so paginatedDocuments/totalPages below don't need
  // to change.
  get filteredDocuments() {
    return this.documents;
  }

  renderAllCharts() {
    this.renderDonutChart();
    this.renderVendorChart();
    this.renderTrendChart();
  }

  // A. Transaction Status Distribution
  renderDonutChart() {
    if (!this.donutChartRef) return;
    if (this.donutChartInstance) this.donutChartInstance.destroy();

    const pending = this.totalPackages - this.totalApproved - this.totalReturned - this.totalUnderReview;

    const ctx = this.donutChartRef.nativeElement.getContext('2d');
    this.donutChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Approved', 'Returned', 'Under Review', 'Pending'],
        datasets: [{
          data: [this.totalApproved, this.totalReturned, this.totalUnderReview, pending],
          backgroundColor: [COLOR_APPROVED, COLOR_RETURNED, COLOR_REVIEW, COLOR_PENDING],
          borderWidth: 0,
          hoverOffset: 6
        }]
      },
      options: {
        cutout: '70%',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'bottom' as const,
            labels: { boxWidth: 10, padding: 10, font: { size: 11 } }
          }
        }
      }
    });
  }

  // C. Top Vendors by Transaction Volume — counts grouped rows (one per
  // package/standalone invoice), never the underlying PO/GR supporting
  // files.
  renderVendorChart() {
    if (!this.vendorChartRef) return;
    if (this.vendorChartInstance) this.vendorChartInstance.destroy();

    const vendorCounts: { [key: string]: number } = {};
    this.documents.forEach((row: any) => {
      const vendor = row.vendorName && row.vendorName !== '-'
        ? row.vendorName.substring(0, 24)
        : 'Unknown';
      vendorCounts[vendor] = (vendorCounts[vendor] || 0) + 1;
    });

    const sorted = Object.entries(vendorCounts).sort((a, b) => b[1] - a[1]);
    const top5 = sorted.slice(0, 5);
    const others = sorted.slice(5);
    const othersTotal = others.reduce((sum, [, count]) => sum + count, 0);

    const labels = top5.map(([name]) => name);
    const data = top5.map(([, count]) => count);

    if (othersTotal > 0) {
      labels.push('Others');
      data.push(othersTotal);
    }

    const ctx = this.vendorChartRef.nativeElement.getContext('2d');
    this.vendorChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Packages',
          data,
          backgroundColor: VENDOR_SHADES,
          borderRadius: 6,
          borderSkipped: false,
        }]
      },
      options: {
        indexAxis: 'y' as const,
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false }
        },
        scales: {
          x: {
            beginAtZero: true,
            ticks: { stepSize: 1 },
            grid: { color: 'rgba(255,255,255,0.06)' }
          },
          y: { grid: { display: false } }
        }
      }
    });
  }

  // B. Processing Trend — last 14 Malaysia calendar days, counting
  // grouped rows (not individual invoices). "Uploaded" buckets each
  // row by its newest member's uploaded_at; "Approved"/"Returned"
  // bucket a row by its latestReviewedAt when its aggregate
  // auditStatusLabel is that status. Reuses the existing uploaded_at/
  // reviewed_at timestamps and the app's shared Malaysia-date-key
  // grouping (src/app/shared/datetime.util.ts) — no new date-bucketing
  // logic.
  renderTrendChart() {
    if (!this.trendChartRef) return;
    if (this.trendChartInstance) this.trendChartInstance.destroy();

    const days: string[] = [];
    const today = new Date();
    for (let i = 13; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      const key = toMalaysiaDateKey(d);
      if (key) days.push(key);
    }

    const uploaded: { [key: string]: number } = {};
    const approved: { [key: string]: number } = {};
    const returned: { [key: string]: number } = {};
    this.documents.forEach((row: any) => {
      const uploadKey = toMalaysiaDateKey(row.uploadedAt);
      if (uploadKey) uploaded[uploadKey] = (uploaded[uploadKey] || 0) + 1;

      if (row.latestReviewedAt) {
        const reviewKey = toMalaysiaDateKey(row.latestReviewedAt);
        if (reviewKey) {
          if (row.auditStatusLabel === 'Approved') approved[reviewKey] = (approved[reviewKey] || 0) + 1;
          if (row.auditStatusLabel === 'Returned') returned[reviewKey] = (returned[reviewKey] || 0) + 1;
        }
      }
    });

    const labels = days.map(key => {
      const [, m, d] = key.split('-');
      return `${d}/${m}`;
    });

    const ctx = this.trendChartRef.nativeElement.getContext('2d');
    this.trendChartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Uploaded',
            data: days.map(key => uploaded[key] || 0),
            borderColor: COLOR_BRAND, backgroundColor: COLOR_BRAND,
            tension: 0.3, pointRadius: 2, borderWidth: 2,
          },
          {
            label: 'Approved',
            data: days.map(key => approved[key] || 0),
            borderColor: COLOR_APPROVED, backgroundColor: COLOR_APPROVED,
            tension: 0.3, pointRadius: 2, borderWidth: 2,
          },
          {
            label: 'Returned',
            data: days.map(key => returned[key] || 0),
            borderColor: COLOR_RETURNED, backgroundColor: COLOR_RETURNED,
            tension: 0.3, pointRadius: 2, borderWidth: 2,
          },
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: 'bottom' as const, labels: { boxWidth: 10, padding: 10, font: { size: 11 } } }
        },
        scales: {
          y: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 }, grid: { color: 'rgba(255,255,255,0.06)' } },
          x: { grid: { display: false }, ticks: { font: { size: 9 } } }
        }
      }
    });
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  // Action — a package row goes to the existing Finance transaction
  // detail page (finance/transactions/detail?id=<transaction_package_
  // id>, the same page Finance Transactions already links to); a
  // standalone invoice goes to the existing single-document detail
  // page (finance/corrections/detail?document_id=X, which already
  // loads original invoice info + correction history + PO/GR status
  // for ANY document regardless of status). No new endpoint, no new
  // page.
  viewRow(row: any) {
    if (row.packageId) {
      this.router.navigate(['/finance/transactions/detail'], { queryParams: { id: row.packageId } });
    } else {
      this.router.navigate(['/finance/corrections/detail'], { queryParams: { document_id: row.documentIds[0] } });
    }
  }

  // Pagination
  currentPage: number = 1;
  pageSize: number = 5;
  Math = Math;

  get paginatedDocuments() {
    const start = (this.currentPage - 1) * this.pageSize;
    const end = start + this.pageSize;
    return this.filteredDocuments.slice(start, end);
  }

  get totalPages(): number {
    return Math.ceil(this.filteredDocuments.length / this.pageSize);
  }

  get pageNumbers(): number[] {
    return Array.from({ length: this.totalPages }, (_, i) => i + 1);
  }

  goToPage(page: number) {
    if (page >= 1 && page <= this.totalPages) {
      this.currentPage = page;
    }
  }

  exportReport() {
    const headers = ['Invoice / Package', 'Related Documents', 'Vendor', 'Amount', 'Upload Date', 'Match Status', 'Audit Status', 'Latest Remark'];
    const rows = this.documents.map((d: any) => [
      d.invoiceLabel || '-',
      d.relatedDocumentsLabel,
      d.vendorName || '-',
      d.totalAmount != null ? (d.currency || '') + ' ' + d.totalAmount : '-',
      this.formatDate(d.uploadedAt),
      d.matchStatusLabel,
      d.auditStatusLabel,
      d.latestRemark || '-'
    ]);

    const csv = [headers, ...rows].map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `AuditLens_Report_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
}
