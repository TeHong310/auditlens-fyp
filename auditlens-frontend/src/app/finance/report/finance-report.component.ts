import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { FormsModule } from '@angular/forms';
import { environment } from '../../../environments/environment';
import { FinanceNotificationBellComponent } from '../shared/finance-notification-bell.component';
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

@Component({
  selector: 'app-finance-report',
  standalone: true,
  imports: [CommonModule, FormsModule, FinanceNotificationBellComponent, FinanceUserMenuComponent],
  templateUrl: './finance-report.component.html',
  styleUrls: ['./finance-report.component.css']
})
export class FinanceReportComponent implements OnInit, AfterViewInit {
  @ViewChild('donutChart') donutChartRef!: ElementRef;
  @ViewChild('vendorChart') vendorChartRef!: ElementRef;
  @ViewChild('trendChart') trendChartRef!: ElementRef;

  // Deduped: one row per document_id (see dedupeByDocument below).
  documents: any[] = [];
  // As returned by the API, unmodified — GET /reviews/finance-report
  // LEFT JOINs review_records, so a document reviewed more than once
  // (sent back, then later approved) comes back as multiple rows.
  // Needed only for the Processing Trend's Approved/Returned event
  // counts, where each row genuinely represents one distinct review
  // action on its own date.
  private rawDocuments: any[] = [];
  isLoading: boolean = false;
  chartReady: boolean = false;
  searchText: string = '';

  // KPIs
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
    this.loadReport();
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
        this.rawDocuments = res.documents;
        this.documents = this.dedupeByDocument(res.documents);

        // "Transaction Packages" = count of invoice-level records this
        // endpoint already returns. Its query selects FROM documents
        // only (POs/GRs live in their own tables and are never joined
        // in as extra rows here — see purchase_order_number/
        // goods_receipt_number below, which are single columns, not
        // extra rows) — so this was never inflated by counting a PO or
        // GR as a separate "package" in the first place.
        this.totalPackages = this.documents.length;
        this.totalApproved = this.documents.filter((d: any) => d.status === 'approved').length;
        this.totalReturned = this.documents.filter((d: any) => d.status === 'returned').length;
        this.totalUnderReview = this.documents.filter((d: any) => d.status === 'under_review').length;

        const withOcr = this.documents.filter((d: any) => d.ocr_confidence != null);
        if (withOcr.length > 0) {
          const sum = withOcr.reduce((acc: number, d: any) => acc + parseFloat(d.ocr_confidence), 0);
          this.avgOcrConfidence = Math.round(sum / withOcr.length);
        }

        this.isLoading = false;
        this.chartReady = true;
        this.cdr.detectChanges();
        setTimeout(() => this.renderAllCharts(), 200);
      },
      error: () => { this.isLoading = false; }
    });
  }

  // Keeps the row with the most recent reviewed_at (its Latest Remark)
  // for each document_id — left un-deduped, KPI counts and the table
  // would double/triple-count a document reviewed more than once.
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

  get filteredDocuments() {
    if (!this.searchText) return this.documents;
    return this.documents.filter(d =>
      d.file_name?.toLowerCase().includes(this.searchText.toLowerCase()) ||
      d.invoice_number?.toLowerCase().includes(this.searchText.toLowerCase()) ||
      d.vendor_name?.toLowerCase().includes(this.searchText.toLowerCase())
    );
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

  // C. Top Vendors by Transaction Volume — counts deduped documents
  // (one per invoice/package), never the underlying PO/GR supporting
  // files.
  renderVendorChart() {
    if (!this.vendorChartRef) return;
    if (this.vendorChartInstance) this.vendorChartInstance.destroy();

    const vendorCounts: { [key: string]: number } = {};
    this.documents.forEach(doc => {
      const vendor = doc.vendor_name
        ? doc.vendor_name.substring(0, 24)
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

  // B. Processing Trend — last 14 Malaysia calendar days. "Uploaded"
  // counts each document once (deduped documents, by uploaded_at);
  // "Approved"/"Returned" count actual review EVENTS from the raw,
  // un-deduped rows — a document sent back then later approved
  // genuinely has both events, on their own separate days. Reuses the
  // existing uploaded_at/reviewed_at timestamps and the app's shared
  // Malaysia-date-key grouping (src/app/shared/datetime.util.ts, the
  // same utility Record Detail/Report pages elsewhere already use for
  // this exact purpose) — no new date-bucketing logic.
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
    this.documents.forEach(doc => {
      const key = toMalaysiaDateKey(doc.uploaded_at);
      if (key) uploaded[key] = (uploaded[key] || 0) + 1;
    });

    const approved: { [key: string]: number } = {};
    const returned: { [key: string]: number } = {};
    this.rawDocuments.forEach(doc => {
      if (!doc.reviewed_at) return;
      const key = toMalaysiaDateKey(doc.reviewed_at);
      if (!key) return;
      if (doc.action === 'approved') approved[key] = (approved[key] || 0) + 1;
      if (doc.action === 'returned') returned[key] = (returned[key] || 0) + 1;
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

  // Audit Status — collapses every in-process document.status value
  // (ocr_processing, ocr_done, resubmitted, etc.) into the 4 values
  // this redesign specifies.
  getStatusClass(status: string): string {
    switch (status) {
      case 'approved': return 'badge-approved';
      case 'returned': return 'badge-returned';
      case 'under_review': return 'badge-review';
      default: return 'badge-pending';
    }
  }

  getStatusLabel(status: string): string {
    switch (status) {
      case 'approved': return 'Approved';
      case 'returned': return 'Returned';
      case 'under_review': return 'Under Review';
      default: return 'Pending';
    }
  }

  // Match Status — two-step: whether PO/GR are ACTUALLY linked
  // (purchase_order_number/goods_receipt_number, the real linkage the
  // backend now joins) decides "Missing Documents" FIRST, independent
  // of overall_status; only once both are present does overall_status
  // — the real, currently-active matching result from routes/
  // auditor.py::build_comparison()/_matching_status_for_comparison()
  // ('PASS'/'REVIEW'/'PARTIAL'/'FAIL', or 'PENDING' when matching
  // hasn't produced a result yet) — decide Full Match/Review Required/
  // Mismatch/Pending. Never a new score or formula, and "Missing
  // Documents" is never the fallback for a null/unknown status when
  // both documents are actually present.
  private hasBothSupportingDocs(doc: any): boolean {
    return !!doc.purchase_order_number && !!doc.goods_receipt_number;
  }

  getMatchStatusLabel(doc: any): string {
    if (!this.hasBothSupportingDocs(doc)) return 'Missing Documents';
    if (doc.overall_status === 'PASS') return 'Full Match';
    if (doc.overall_status === 'REVIEW' || doc.overall_status === 'PARTIAL') return 'Review Required';
    if (doc.overall_status === 'FAIL') return 'Mismatch';
    return 'Pending';
  }

  getMatchStatusClass(doc: any): string {
    if (!this.hasBothSupportingDocs(doc)) return 'badge-returned';
    if (doc.overall_status === 'PASS') return 'badge-approved';
    if (doc.overall_status === 'REVIEW' || doc.overall_status === 'PARTIAL') return 'badge-review';
    if (doc.overall_status === 'FAIL') return 'badge-returned';
    return 'badge-pending';
  }

  // Related Documents — purchase_order_number/goods_receipt_number are
  // the actual linked PO/GR numbers (backend now joins them the same
  // way routes/auditor.py::build_comparison() already does, by
  // document_id, latest row wins) — never a placeholder or guess.
  getRelatedDocuments(doc: any): string {
    const po = doc.purchase_order_number ? `PO: ${doc.purchase_order_number}` : 'PO: Not Uploaded';
    const gr = doc.goods_receipt_number ? `GR: ${doc.goods_receipt_number}` : 'GR: Not Uploaded';
    return `${po} · ${gr}`;
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  // View action — reuses the existing Finance single-document detail
  // page (finance/corrections/detail?document_id=X), which already
  // loads original invoice info + correction history + PO/GR status
  // for ANY document regardless of status (see finance-correction-
  // detail.component.ts's own loadAll()) — not exclusive to returned/
  // correction-flow documents, so it works as a single "View" target
  // for every row here without needing a transaction_package_id (this
  // report has none, and adding one would mean another backend change
  // beyond the PO/GR numbers already added).
  viewDocument(doc: any) {
    this.router.navigate(['/finance/corrections/detail'], { queryParams: { document_id: doc.document_id } });
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
    const headers = ['Invoice No', 'Vendor', 'Amount', 'Upload Date', 'Related Documents', 'Match Status', 'Audit Status', 'Latest Remark'];
    const rows = this.documents.map(d => [
      d.invoice_number || '-',
      d.vendor_name || '-',
      d.total_amount ? (d.currency || '') + ' ' + d.total_amount : '-',
      this.formatDate(d.uploaded_at),
      this.getRelatedDocuments(d),
      this.getMatchStatusLabel(d),
      this.getStatusLabel(d.status),
      d.comments || '-'
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
