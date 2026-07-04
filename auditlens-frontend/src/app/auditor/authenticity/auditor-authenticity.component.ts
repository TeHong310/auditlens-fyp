import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type Filter = 'all' | 'passed' | 'warning';

@Component({
  selector: 'app-auditor-authenticity',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './auditor-authenticity.component.html',
  styleUrls: ['./auditor-authenticity.component.css']
})
export class AuditorAuthenticityComponent implements OnInit {

  checks: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';
  activeFilter: Filter = 'all';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadChecks();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadChecks() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any[]>(`${this.apiUrl}/authenticity`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.checks = res || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load authenticity checks.';
        this.cdr.detectChanges();
      }
    });
  }

  setFilter(f: Filter) {
    this.activeFilter = f;
  }

  get filteredChecks() {
    if (this.activeFilter === 'passed') return this.checks.filter(c => c.authenticity_status === 'passed');
    if (this.activeFilter === 'warning') return this.checks.filter(c => c.authenticity_status === 'warning');
    return this.checks;
  }

  get passedCount(): number {
    return this.checks.filter(c => c.authenticity_status === 'passed').length;
  }

  get warningCount(): number {
    return this.checks.filter(c => c.authenticity_status === 'warning').length;
  }

  viewDocument(check: any) {
    this.router.navigate(['/auditor/record-detail'], {
      queryParams: { document_id: check.document_id }
    });
  }

  docTypeLabel(type: string): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'PO';
    if (type === 'gr') return 'GR';
    return type || 'Unknown';
  }

  uploadSourceIcon(source: string): string {
    if (source === 'phone_photo') return '📱';
    if (source === 'scanned') return '🖨️';
    if (source === 'digital_native') return '💻';
    if (source === 'webcam') return '📷';
    return '❓';
  }

  uploadSourceLabel(source: string): string {
    if (source === 'phone_photo') return 'Phone Photo';
    if (source === 'scanned') return 'Scanned';
    if (source === 'digital_native') return 'Digital Native';
    if (source === 'webcam') return 'Webcam';
    return 'Unknown';
  }

  warningReason(check: any): string {
    const type = (check.document_type || '').toLowerCase();
    const missing: string[] = [];
    if (!check.has_company_name) missing.push('company name');
    if (type === 'invoice' && !check.has_company_chop && !check.has_signature) {
      missing.push('chop/signature');
    }
    if (missing.length === 0) return '';
    return `Missing ${missing.join(' and ')} (required for ${this.docTypeLabel(check.document_type)})`;
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
