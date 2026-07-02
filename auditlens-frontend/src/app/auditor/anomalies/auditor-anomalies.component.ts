import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type Severity = 'all' | 'high' | 'medium' | 'low';
type AnomalyType = 'all' | 'amount' | 'round' | 'weekend' | 'duplicate';
type Status = 'pending' | 'reviewed' | 'dismissed';

@Component({
  selector: 'app-auditor-anomalies',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-anomalies.component.html',
  styleUrls: ['./auditor-anomalies.component.css']
})
export class AuditorAnomaliesComponent implements OnInit {

  anomalies: any[] = [];
  stats: any = { total: 0, by_severity: { high: 0, medium: 0, low: 0 }, by_type: { amount: 0, round: 0, weekend: 0, duplicate: 0 } };

  isLoading: boolean = false;
  errorMessage: string = '';
  actionInFlight: number | null = null;

  activeSeverity: Severity = 'all';
  activeType: AnomalyType = 'all';
  activeStatus: Status = 'pending';

  severityFilters: { key: Severity; label: string; icon: string }[] = [
    { key: 'all', label: 'All', icon: '' },
    { key: 'high', label: 'High', icon: '🔴' },
    { key: 'medium', label: 'Med', icon: '🟡' },
    { key: 'low', label: 'Low', icon: '🟠' },
  ];

  typeFilters: { key: AnomalyType; label: string; icon: string }[] = [
    { key: 'all', label: 'All', icon: '' },
    { key: 'amount', label: 'Amount', icon: '💰' },
    { key: 'round', label: 'Round', icon: '🎯' },
    { key: 'weekend', label: 'Weekend', icon: '📅' },
    { key: 'duplicate', label: 'Dup', icon: '🔁' },
  ];

  statusFilters: { key: Status; label: string }[] = [
    { key: 'pending', label: 'Pending' },
    { key: 'reviewed', label: 'Reviewed' },
    { key: 'dismissed', label: 'Dismissed' },
  ];

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadStats();
    this.loadAnomalies();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadStats() {
    this.http.get<any>(`${this.apiUrl}/anomalies/stats`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.stats = res;
        this.cdr.detectChanges();
      },
      error: () => { /* header/chip counts are non-critical; list load surfaces real errors */ }
    });
  }

  loadAnomalies() {
    this.isLoading = true;
    this.errorMessage = '';
    const url = `${this.apiUrl}/anomalies?severity=${this.activeSeverity}&type=${this.activeType}&status=${this.activeStatus}&limit=100`;
    this.http.get<any[]>(url, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.anomalies = res || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load anomalies.';
        this.cdr.detectChanges();
      }
    });
  }

  setSeverity(s: Severity) {
    this.activeSeverity = s;
    this.loadAnomalies();
  }

  setType(t: AnomalyType) {
    this.activeType = t;
    this.loadAnomalies();
  }

  setStatus(s: Status) {
    this.activeStatus = s;
    this.loadAnomalies();
  }

  investigate(anomaly: any) {
    this.router.navigate(['/auditor/record-detail'], {
      queryParams: { document_id: anomaly.invoice_document_id }
    });
  }

  review(anomaly: any, status: 'reviewed' | 'dismissed') {
    this.actionInFlight = anomaly.anomaly_id;
    this.http.post<any>(`${this.apiUrl}/anomalies/${anomaly.anomaly_id}/review`,
      { status },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.actionInFlight = null;
        this.anomalies = this.anomalies.filter(a => a.anomaly_id !== anomaly.anomaly_id);
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.actionInFlight = null;
        this.errorMessage = err.error?.error || 'Failed to update anomaly.';
        this.cdr.detectChanges();
      }
    });
  }

  severityIcon(sev: string): string {
    if (sev === 'high') return '🔴';
    if (sev === 'medium') return '🟡';
    return '🟠';
  }

  typeIcon(type: string): string {
    if (type === 'amount') return '💰';
    if (type === 'round') return '🎯';
    if (type === 'weekend') return '📅';
    if (type === 'duplicate') return '🔁';
    return '❓';
  }

  typeLabel(type: string): string {
    if (type === 'amount') return 'Amount Anomaly';
    if (type === 'round') return 'Round Amount';
    if (type === 'weekend') return 'Weekend Transaction';
    if (type === 'duplicate') return 'Duplicate Suspicion';
    return 'Anomaly';
  }

  formatAmount(v: any): string {
    if (v === null || v === undefined) return '-';
    return 'RM ' + Number(v).toLocaleString('en-MY', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  patternLines(anomaly: any): { label: string; value: string }[] {
    const p = anomaly.detected_pattern || {};
    switch (anomaly.anomaly_type) {
      case 'amount': {
        const ratio = p.mean ? (p.current / p.mean) : null;
        return [
          { label: 'Current', value: this.formatAmount(p.current) },
          { label: 'Vendor 90-day avg', value: `${this.formatAmount(p.mean)} (${ratio ? ratio.toFixed(1) + 'x higher' : '-'})` },
          { label: 'Sample size', value: `${p.sample_size} prior invoice(s)` },
        ];
      }
      case 'round':
        return [
          { label: 'Amount', value: this.formatAmount(p.amount) },
          { label: 'Pattern', value: 'Exact multiple of RM 500' },
        ];
      case 'weekend':
        return [
          { label: 'Invoice Date', value: p.date },
          { label: 'Day of Week', value: p.day_of_week },
        ];
      case 'duplicate':
        return [
          { label: 'Matched Invoice', value: p.matched_invoice_no || 'Unknown' },
          { label: 'Matched Date', value: p.matched_date },
          { label: 'Days Apart', value: `${p.days_apart}` },
          { label: 'Amount Diff', value: `${p.amount_diff_pct}%` },
        ];
      default:
        return [];
    }
  }

  relativeTime(dateStr: string): string {
    if (!dateStr) return '-';
    const diffMs = Date.now() - new Date(dateStr).getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr} hr ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay === 1) return '1 day ago';
    if (diffDay < 30) return `${diffDay} days ago`;
    return new Date(dateStr).toLocaleDateString('en-MY', { day: '2-digit', month: 'short', year: 'numeric' });
  }
}
