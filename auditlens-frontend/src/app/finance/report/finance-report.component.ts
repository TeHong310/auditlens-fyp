import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { FormsModule } from '@angular/forms';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

@Component({
  selector: 'app-finance-report',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './finance-report.component.html',
  styleUrls: ['./finance-report.component.css']
})
export class FinanceReportComponent implements OnInit, AfterViewInit {
  @ViewChild('donutChart') donutChartRef!: ElementRef;
  @ViewChild('vendorChart') vendorChartRef!: ElementRef;
  @ViewChild('matchChart') matchChartRef!: ElementRef;

  documents: any[] = [];
  isLoading: boolean = false;
  chartReady: boolean = false;
  searchText: string = '';

  // Stats
  totalDocuments: number = 0;
  totalApproved: number = 0;
  totalReturned: number = 0;
  totalUnderReview: number = 0;
  avgMatchScore: number = 0;

  private donutChartInstance: any = null;
  private vendorChartInstance: any = null;
  private matchChartInstance: any = null;

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
        this.documents = res.documents;

        this.totalDocuments = res.documents.length;
        this.totalApproved = res.documents.filter((d: any) => d.status === 'approved').length;
        this.totalReturned = res.documents.filter((d: any) => d.status === 'returned').length;
        this.totalUnderReview = res.documents.filter((d: any) => d.status === 'under_review').length;

        const withScore = res.documents.filter((d: any) => d.match_score != null);
        if (withScore.length > 0) {
          const sum = withScore.reduce((acc: number, d: any) => acc + parseFloat(d.match_score), 0);
          this.avgMatchScore = Math.round(sum / withScore.length);
        }

        this.isLoading = false;
        this.chartReady = true;
        this.cdr.detectChanges();
        setTimeout(() => this.renderAllCharts(), 200);
      },
      error: () => { this.isLoading = false; }
    });
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
    this.renderMatchChart();
  }

  renderDonutChart() {
    if (!this.donutChartRef) return;
    if (this.donutChartInstance) this.donutChartInstance.destroy();

    const pending = this.totalDocuments - this.totalApproved - this.totalReturned - this.totalUnderReview;

    const ctx = this.donutChartRef.nativeElement.getContext('2d');
    this.donutChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Approved', 'Returned', 'Under Review', 'Pending'],
        datasets: [{
          data: [this.totalApproved, this.totalReturned, this.totalUnderReview, pending],
          backgroundColor: ['#10B981', '#EF4444', '#F59E0B', '#4A90D9'],
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

  renderVendorChart() {
    if (!this.vendorChartRef) return;
    if (this.vendorChartInstance) this.vendorChartInstance.destroy();

    // Group by vendor
    const vendorCounts: { [key: string]: number } = {};
    this.documents.forEach(doc => {
      const vendor = doc.vendor_name
        ? doc.vendor_name.substring(0, 20)
        : 'Unknown';
      vendorCounts[vendor] = (vendorCounts[vendor] || 0) + 1;
    });

    // Sort by count, take top 5, rest = Others
    const sorted = Object.entries(vendorCounts)
      .sort((a, b) => b[1] - a[1]);

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
          label: 'Documents',
          data,
          backgroundColor: [
            '#2E6DA4', '#357ABD', '#4A90D9',
            '#5BA3E0', '#78B8F0', '#9CA3AF'
          ],
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
            grid: { color: '#F3F4F6' }
          },
          y: { grid: { display: false } }
        }
      }
    });
  }

  renderMatchChart() {
    if (!this.matchChartRef) return;
    if (this.matchChartInstance) this.matchChartInstance.destroy();

    const withScore = this.documents
      .filter(d => d.match_score != null)
      .slice(0, 10);

    const labels = withScore.map(d =>
      d.invoice_number ? d.invoice_number.substring(0, 15) : d.file_name.substring(0, 15)
    );
    const data = withScore.map(d => parseFloat(d.match_score));

    const colors = data.map(score =>
      score >= 80 ? '#10B981' : score >= 50 ? '#F59E0B' : '#EF4444'
    );

    const ctx = this.matchChartRef.nativeElement.getContext('2d');
    this.matchChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Match Score %',
          data,
          backgroundColor: colors,
          borderRadius: 6,
          borderSkipped: false,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false }
        },
        scales: {
          y: {
            beginAtZero: true,
            max: 100,
            ticks: { stepSize: 20 },
            grid: { color: '#F3F4F6' }
          },
          x: { grid: { display: false } }
        }
      }
    });
  }

  getStatusClass(status: string): string {
    switch (status) {
      case 'approved': return 'badge-matched';
      case 'returned': return 'badge-returned';
      case 'under_review': return 'badge-review';
      case 'ocr_done': return 'badge-processed';
      default: return 'badge-pending';
    }
  }

  getStatusLabel(status: string): string {
    switch (status) {
      case 'approved': return 'Approved';
      case 'returned': return 'Returned';
      case 'under_review': return 'Under Review';
      case 'ocr_done': return 'OCR Done';
      default: return status;
    }
  }

  getMatchClass(score: number): string {
    if (score == null) return 'badge-pending';
    if (score >= 80) return 'badge-matched';
    if (score >= 50) return 'badge-review';
    return 'badge-returned';
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
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
    const headers = ['File Name', 'Invoice No', 'Vendor', 'Amount', 'Status', 'Match Score', 'Comments'];
    const rows = this.documents.map(d => [
      d.file_name,
      d.invoice_number || '-',
      d.vendor_name || '-',
      d.total_amount || '-',
      this.getStatusLabel(d.status),
      d.match_score != null ? d.match_score + '%' : '-',
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