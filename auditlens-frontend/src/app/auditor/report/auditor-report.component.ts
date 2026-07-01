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

  stats: any = { approved: 0, sent_back: 0, pending: 0, exceptions: 0 };
  timeline: any[] = [];
  isLoadingSummary: boolean = false;
  summaryError: string = '';

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
    if (action === 'approved') return '✅ Approved';
    if (action === 'sent_back') return '↩️ Sent Back';
    return '⚠️ Need Review';
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
