import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type Filter = 'all' | 'complete' | 'incomplete';

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

  isComplete(check: any): boolean {
    return !!(check.has_company_chop && check.has_company_logo && check.has_company_name);
  }

  get filteredChecks() {
    if (this.activeFilter === 'complete') return this.checks.filter(c => this.isComplete(c));
    if (this.activeFilter === 'incomplete') return this.checks.filter(c => !this.isComplete(c));
    return this.checks;
  }

  get completeCount(): number {
    return this.checks.filter(c => this.isComplete(c)).length;
  }

  get incompleteCount(): number {
    return this.checks.length - this.completeCount;
  }

  viewDocument(check: any) {
    this.router.navigate(['/auditor/record-detail'], {
      queryParams: { document_id: check.document_id }
    });
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
