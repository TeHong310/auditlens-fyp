import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

const STATUS_LABELS: Record<string, string> = {
  draft: 'Draft',
  waiting_documents: 'Waiting Documents',
  processing: 'Processing',
  completed: 'Completed',
};

// Enterprise V3 Phase 5 — Finance Transaction Package list. Reads GET
// /transaction-packages (Finance's own packages only) — no new
// calculation here, document_count/supplier/status are already
// computed server-side by helpers/transaction_packages.py::list_packages.
@Component({
  selector: 'app-finance-transactions',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './finance-transactions.component.html',
  styleUrls: ['./finance-transactions.component.css']
})
export class FinanceTransactionsComponent implements OnInit {
  packages: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';
  successMessage: string = '';
  searchText: string = '';

  // ── Delete (Phase 15) ──
  packagePendingDelete: any = null;
  isDeleting: boolean = false;
  deleteError: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    this.loadPackages();
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadPackages() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any[]>(`${this.apiUrl}/transaction-packages`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.packages = res;
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load transaction packages.';
        this.cdr.detectChanges();
      }
    });
  }

  get filteredPackages() {
    if (!this.searchText) return this.packages;
    const q = this.searchText.toLowerCase();
    return this.packages.filter(p =>
      p.package_name?.toLowerCase().includes(q) || p.supplier?.toLowerCase().includes(q)
    );
  }

  statusLabel(status: string): string {
    return STATUS_LABELS[status] || status;
  }

  statusClass(status: string): string {
    return 'status-' + (status || 'draft');
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

  openPackage(pkg: any) {
    this.router.navigate(['/finance/transactions/detail'], { queryParams: { id: pkg.id } });
  }

  createNewPackage() {
    this.router.navigate(['/finance/transactions/create']);
  }

  // ── Delete (Phase 15) — management feature only. Calls the new
  // DELETE /transaction-packages/<id>/force endpoint (a separate route
  // from the existing, automatic "empty ghost package" cleanup DELETE
  // /transaction-packages/<id>, which must keep its own strict
  // empty-only behavior unchanged). Every deletion decision (what's
  // safe to remove vs. shared with another package) is made entirely
  // server-side — this component only confirms intent and refreshes. ──

  confirmDelete(pkg: any, event: Event) {
    event.stopPropagation();
    this.packagePendingDelete = pkg;
    this.deleteError = '';
  }

  cancelDelete() {
    this.packagePendingDelete = null;
    this.deleteError = '';
  }

  deletePackage() {
    if (!this.packagePendingDelete || this.isDeleting) return;
    const pkg = this.packagePendingDelete;
    this.isDeleting = true;
    this.deleteError = '';

    this.http.delete<any>(`${this.apiUrl}/transaction-packages/${pkg.id}/force`, { headers: this.getHeaders() }).subscribe({
      next: () => {
        this.isDeleting = false;
        this.packagePendingDelete = null;
        this.successMessage = `"${pkg.package_name}" and its documents were deleted.`;
        this.cdr.detectChanges();
        setTimeout(() => { this.successMessage = ''; this.cdr.detectChanges(); }, 4000);
        this.loadPackages();
      },
      error: (err) => {
        this.isDeleting = false;
        this.deleteError = err.error?.error || 'Failed to delete transaction package.';
        this.cdr.detectChanges();
      }
    });
  }
}
