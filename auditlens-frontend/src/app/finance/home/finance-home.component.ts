import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

@Component({
  selector: 'app-finance-home',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './finance-home.component.html',
  styleUrls: ['./finance-home.component.css']
})
export class FinanceHomeComponent implements OnInit, AfterViewInit {
  @ViewChild('barChart') barChartRef!: ElementRef;
  @ViewChild('donutChart') donutChartRef!: ElementRef;
  @ViewChild('lineChart') lineChartRef!: ElementRef;

  documents: any[] = [];
  allDocuments: any[] = [];
  totalUploaded: number = 0;
  totalOcrProcessed: number = 0;
  totalUnderReview: number = 0;
  totalApproved: number = 0;
  totalReturned: number = 0;
  avgConfidence: number = 0;
  isLoading: boolean = false;
  chartReady: boolean = false;

  private barChartInstance: any = null;
  private donutChartInstance: any = null;
  private lineChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    this.loadDocuments();
  }

  ngAfterViewInit() {
    if (this.chartReady) {
      this.renderAllCharts();
    }
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

        // Recent uploads table — current month only, last 5
        this.documents = thisMonthDocs.slice(0, 5);

        this.isLoading = false;
        this.chartReady = true;
        this.cdr.detectChanges();
        setTimeout(() => this.renderAllCharts(), 200);
      },
      error: () => { this.isLoading = false; }
    });
  }

  renderAllCharts() {
    this.renderBarChart();
    this.renderDonutChart();
    this.renderLineChart();
  }

  renderBarChart() {
    if (!this.barChartRef) return;
    if (this.barChartInstance) this.barChartInstance.destroy();

    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    const uploadCounts: { [key: string]: number } = {};
    this.allDocuments.forEach(doc => {
      const date = new Date(doc.uploaded_at);
      const key = months[date.getMonth()];
      uploadCounts[key] = (uploadCounts[key] || 0) + 1;
    });

    const approvedCounts: { [key: string]: number } = {};
    this.allDocuments.filter(d => d.status === 'approved').forEach(doc => {
      const date = new Date(doc.uploaded_at);
      const key = months[date.getMonth()];
      approvedCounts[key] = (approvedCounts[key] || 0) + 1;
    });

    const uploadData = months.map(m => uploadCounts[m] || 0);
    const approvedData = months.map(m => approvedCounts[m] || 0);

    const ctx = this.barChartRef.nativeElement.getContext('2d');
    this.barChartInstance = new Chart(ctx, {
      data: {
        labels: months,
        datasets: [
          {
            type: 'bar' as const,
            label: 'Uploaded',
            data: uploadData,
            backgroundColor: 'rgba(74, 144, 217, 0.7)',
            borderRadius: 5,
            borderSkipped: false,
            yAxisID: 'y',
          },
          {
            type: 'line' as const,
            label: 'Approved',
            data: approvedData,
            borderColor: '#10B981',
            backgroundColor: 'rgba(16, 185, 129, 0.1)',
            borderWidth: 2,
            pointBackgroundColor: '#10B981',
            pointRadius: 4,
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
          legend: {
            display: true,
            position: 'top' as const,
            labels: { boxWidth: 10, font: { size: 11 }, padding: 12 }
          },
          tooltip: { mode: 'index' as const, intersect: false }
        },
        scales: {
          y: {
            beginAtZero: true,
            ticks: { stepSize: 1 },
            grid: { color: '#F3F4F6' }
          },
          x: { grid: { display: false } }
        }
      }
    });
  }

  renderDonutChart() {
    if (!this.donutChartRef) return;
    if (this.donutChartInstance) this.donutChartInstance.destroy();

    // Current month only
    const now = new Date();
    const thisMonthDocs = this.allDocuments.filter(d => {
      const date = new Date(d.uploaded_at);
      return date.getMonth() === now.getMonth() &&
        date.getFullYear() === now.getFullYear();
    });

    const ocrDone = thisMonthDocs.filter(d =>
      ['ocr_done'].includes(d.status)).length;
    const underReview = thisMonthDocs.filter(d => d.status === 'under_review').length;
    const approved = thisMonthDocs.filter(d => d.status === 'approved').length;
    const returned = thisMonthDocs.filter(d => d.status === 'returned').length;

    const ctx = this.donutChartRef.nativeElement.getContext('2d');
    this.donutChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['OCR Done', 'Under Review', 'Approved', 'Returned'],
        datasets: [{
          data: [ocrDone, underReview, approved, returned],
          backgroundColor: ['#4A90D9', '#F59E0B', '#10B981', '#EF4444'],
          borderWidth: 0,
          hoverOffset: 6
        }]
      },
      options: {
        cutout: '72%',
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

  renderLineChart() {
    if (!this.lineChartRef) return;
    if (this.lineChartInstance) this.lineChartInstance.destroy();

    // Current month only, last 10
    const now = new Date();
    const thisMonthDocs = this.allDocuments.filter(d => {
      const date = new Date(d.uploaded_at);
      return date.getMonth() === now.getMonth() &&
        date.getFullYear() === now.getFullYear();
    });

    const last10 = thisMonthDocs
      .filter(d => d.ocr_confidence)
      .slice(-10);

    const labels = last10.map((_, i) => `Doc ${i + 1}`);
    const data = last10.map(d => parseFloat(d.ocr_confidence));

    const ctx = this.lineChartRef.nativeElement.getContext('2d');
    this.lineChartInstance = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'OCR Confidence %',
          data,
          borderColor: '#6366F1',
          backgroundColor: 'rgba(99, 102, 241, 0.1)',
          borderWidth: 2,
          pointBackgroundColor: '#6366F1',
          pointRadius: 4,
          tension: 0.4,
          fill: true,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: {
            min: 0, max: 100,
            ticks: { stepSize: 20 },
            grid: { color: '#F3F4F6' }
          },
          x: { grid: { display: false } }
        }
      }
    });
  }

  viewDocument(doc: any) {
    const token = localStorage.getItem('access_token');
    const url = `${this.apiUrl}/documents/${doc.document_id}/file`;
    fetch(url, { headers: { 'Authorization': `Bearer ${token}` } })
      .then(res => res.blob())
      .then(blob => window.open(URL.createObjectURL(blob), '_blank'))
      .catch(() => alert('Failed to open file.'));
  }

  goToUpload() { this.router.navigate(['/finance/upload']); }
  goToOcrReview() { this.router.navigate(['/finance/ocr-review']); }

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

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }
}