import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type MatchFilter = 'all' | 'PASS' | 'REVIEW' | 'FAIL';

// Three-Way Matching — a dedicated auditor page listing every
// transaction package/standalone invoice with its match verdict. Reads
// the SAME GET /auditor/transactions endpoint the Auditor Home
// dashboard and Review Queue already use (Phase 6/13) — no new
// matching calculation of its own, matching_status is the existing
// Enterprise Matching V2-derived verdict computed server-side.
@Component({
  selector: 'app-auditor-three-way-matching',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-three-way-matching.component.html',
  styleUrls: ['./auditor-three-way-matching.component.css']
})
export class AuditorThreeWayMatchingComponent implements OnInit {

  isLoading: boolean = false;
  errorMessage: string = '';
  transactions: any[] = [];
  searchText: string = '';
  activeFilter: MatchFilter = 'all';

  // ── Pagination — frontend-only, over the already-loaded/filtered
  // transactions array (no new backend call, no change to the existing
  // search/filter logic above, which paginatedTransactions below reads
  // through). ──
  pageSize = 10;
  currentPage = 1;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadTransactions();
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadTransactions() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any[]>(`${this.apiUrl}/auditor/transactions`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.transactions = res || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load three-way matching data.';
        this.cdr.detectChanges();
      }
    });
  }

  get passCount(): number {
    return this.transactions.filter(t => t.matching_status === 'PASS').length;
  }

  get reviewCount(): number {
    return this.transactions.filter(t => t.matching_status === 'REVIEW').length;
  }

  get failCount(): number {
    return this.transactions.filter(t => t.matching_status === 'FAIL').length;
  }

  setFilter(f: MatchFilter) {
    this.activeFilter = f;
    this.currentPage = 1;
  }

  // Bound alongside the search input's existing [(ngModel)] — keeps
  // that two-way binding exactly as it was, just also resets to page 1
  // whenever the keyword changes so pagination never strands the user
  // on a page that no longer exists for the new, narrower result set.
  onSearchChange() {
    this.currentPage = 1;
  }

  get filteredTransactions() {
    let rows = this.transactions;
    if (this.activeFilter !== 'all') {
      rows = rows.filter(t => t.matching_status === this.activeFilter);
    }
    if (this.searchText.trim()) {
      const q = this.searchText.trim().toLowerCase();
      rows = rows.filter(t =>
        (t.package_name || '').toLowerCase().includes(q) ||
        (t.supplier || '').toLowerCase().includes(q) ||
        (t.invoice_numbers || []).some((n: string) => n.toLowerCase().includes(q))
      );
    }
    return rows;
  }

  // ── Pagination, over filteredTransactions above (so filters/search
  // are always applied before the page slice — pagination narrows an
  // already-filtered list, it never bypasses it). ──

  get totalPages(): number {
    return Math.max(1, Math.ceil(this.filteredTransactions.length / this.pageSize));
  }

  get pageNumbers(): number[] {
    return Array.from({ length: this.totalPages }, (_, i) => i + 1);
  }

  get paginatedTransactions(): any[] {
    const startIndex = (this.currentPage - 1) * this.pageSize;
    return this.filteredTransactions.slice(startIndex, startIndex + this.pageSize);
  }

  goToPage(page: number) {
    if (page < 1 || page > this.totalPages) return;
    this.currentPage = page;
  }

  prevPage() {
    this.goToPage(this.currentPage - 1);
  }

  nextPage() {
    this.goToPage(this.currentPage + 1);
  }

  // Related Docs columns — same fields/helper Auditor Home already
  // reads off this endpoint (invoice_numbers/po_numbers/gr_numbers).
  relatedDocLabel(numbers: string[] | undefined): string {
    return numbers && numbers.length > 0 ? numbers.join(', ') : '-';
  }

  formatAmount(amount: any, currency?: string | null): string {
    if (amount === null || amount === undefined || amount === '') return '-';
    return (currency || 'RM') + ' ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  matchingStatusClass(status: string): string {
    switch (status) {
      case 'PASS':    return 'badge-approved';
      case 'REVIEW':  return 'badge-review';
      case 'FAIL':    return 'badge-failed';
      case 'PARTIAL': return 'badge-resubmitted';
      default:         return 'badge-pending';
    }
  }

  matchingStatusLabel(status: string): string {
    switch (status) {
      case 'PASS':    return 'PASS';
      case 'REVIEW':  return 'REVIEW';
      case 'FAIL':    return 'FAIL';
      case 'PARTIAL': return 'PARTIAL';
      default:         return 'PENDING';
    }
  }

  // View Details — opens the EXISTING, unmodified Three-Way Matching
  // Details page (auditor-matching-details.component.ts), the same
  // navigation Record Detail's own "View Matching Details" button
  // already uses (document_id + transaction_package_id when this
  // invoice belongs to one).
  viewDetails(txn: any) {
    const queryParams: any = { document_id: txn.primary_document_id };
    if (txn.transaction_package_id) queryParams.transaction_package_id = txn.transaction_package_id;
    this.router.navigate(['/auditor/matching-details'], { queryParams });
  }
}
