import { Component, OnInit, AfterViewInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Chart, registerables } from 'chart.js';
import { environment } from '../../../environments/environment';

Chart.register(...registerables);

// Admin Dashboard visualization upgrade — reads the EXISTING GET
// /admin/statistics (now also carrying a small read-only
// monthly_uploads aggregate, added for the Monthly Upload Trend chart
// only) plus the EXISTING, unmodified GET /admin/users and GET
// /admin/documents (already used by the User/Document Management
// pages) for the Recent Users / Recent Documents lists below. No new
// calculation happens here beyond simple client-side sort+slice of
// data those endpoints already return — nothing in this file computes
// business logic of any kind.
@Component({
  selector: 'app-admin-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './admin-dashboard.component.html',
  styleUrls: ['./admin-dashboard.component.css']
})
export class AdminDashboardComponent implements OnInit, AfterViewInit {
  @ViewChild('roleChart') roleChartRef!: ElementRef;
  @ViewChild('statusChart') statusChartRef!: ElementRef;
  @ViewChild('trendChart') trendChartRef!: ElementRef;

  user: any = {};
  isLoading: boolean = false;
  errorMessage: string = '';
  stats: any = null;
  chartReady: boolean = false;

  recentUsers: any[] = [];
  recentDocuments: any[] = [];

  private roleChartInstance: any = null;
  private statusChartInstance: any = null;
  private trendChartInstance: any = null;

  private apiUrl = environment.apiUrl;

  constructor(private http: HttpClient, private cdr: ChangeDetectorRef) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      this.user = JSON.parse(localStorage.getItem('user') || '{}');
    }
    this.loadStatistics();
    this.loadRecentUsers();
    this.loadRecentDocuments();
  }

  ngAfterViewInit() {
    if (this.chartReady) {
      this.renderAllCharts();
    }
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadStatistics() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/admin/statistics`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.stats = res;
        this.isLoading = false;
        this.chartReady = true;
        this.cdr.detectChanges();
        setTimeout(() => this.renderAllCharts(), 150);
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load system statistics.';
        this.cdr.detectChanges();
      }
    });
  }

  loadRecentUsers() {
    this.http.get<any>(`${this.apiUrl}/admin/users`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        const users = res.users || [];
        this.recentUsers = [...users]
          .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
          .slice(0, 5);
        this.cdr.detectChanges();
      },
      error: () => { /* Recent Users is a secondary panel — a failure here shouldn't block the rest of the dashboard */ }
    });
  }

  loadRecentDocuments() {
    this.http.get<any>(`${this.apiUrl}/admin/documents`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        const documents = res.documents || [];
        this.recentDocuments = [...documents]
          .sort((a, b) => new Date(b.uploaded_at).getTime() - new Date(a.uploaded_at).getTime())
          .slice(0, 5);
        this.cdr.detectChanges();
      },
      error: () => { /* Recent Documents is a secondary panel — a failure here shouldn't block the rest of the dashboard */ }
    });
  }

  renderAllCharts() {
    this.renderRoleChart();
    this.renderStatusChart();
    this.renderTrendChart();
  }

  renderRoleChart() {
    if (!this.roleChartRef || !this.stats) return;
    if (this.roleChartInstance) this.roleChartInstance.destroy();

    const u = this.stats.users;
    const ctx = this.roleChartRef.nativeElement.getContext('2d');
    this.roleChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Finance Executive', 'Auditor', 'Admin'],
        datasets: [{
          data: [u.total_finance, u.total_auditors, u.total_admins],
          backgroundColor: ['#4A90D9', '#F59E0B', '#6366F1'],
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

  renderStatusChart() {
    if (!this.statusChartRef || !this.stats) return;
    if (this.statusChartInstance) this.statusChartInstance.destroy();

    const d = this.stats.documents;
    const ctx = this.statusChartRef.nativeElement.getContext('2d');
    this.statusChartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Under Review', 'Approved', 'Returned', 'Resubmitted'],
        datasets: [{
          data: [d.under_review, d.approved, d.returned, d.resubmitted],
          backgroundColor: ['#F59E0B', '#10B981', '#EF4444', '#6366F1'],
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

  renderTrendChart() {
    if (!this.trendChartRef || !this.stats) return;
    if (this.trendChartInstance) this.trendChartInstance.destroy();

    const monthly: { month: string; count: number }[] = this.stats.monthly_uploads || [];
    const labels = monthly.map(m => this.formatMonth(m.month));
    const data = monthly.map(m => m.count);

    const ctx = this.trendChartRef.nativeElement.getContext('2d');
    this.trendChartInstance = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Documents Uploaded',
          data,
          backgroundColor: 'rgba(74, 144, 217, 0.7)',
          borderRadius: 5,
          borderSkipped: false,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
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

  private formatMonth(yyyyMm: string): string {
    if (!yyyyMm) return '';
    const [year, month] = yyyyMm.split('-');
    const date = new Date(Number(year), Number(month) - 1, 1);
    return date.toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
  }

  roleLabel(role: string): string {
    if (role === 'finance_executive') return 'Finance Executive';
    if (role === 'auditor') return 'Auditor';
    if (role === 'admin') return 'Admin';
    return role;
  }

  statusLabel(status: string): string {
    return (status || '').split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  }

  statusBadgeClass(status: string): string {
    if (status === 'approved') return 'badge-approved';
    if (status === 'under_review' || status === 'resubmitted') return 'badge-review';
    if (status === 'returned') return 'badge-returned';
    return 'badge-pending';
  }
}
