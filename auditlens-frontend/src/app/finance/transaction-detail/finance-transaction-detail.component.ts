import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

const STATUS_LABELS: Record<string, string> = {
  draft: 'Draft',
  waiting_documents: 'Waiting Documents',
  processing: 'Processing',
  completed: 'Completed',
};

// Enterprise V3 Phase 5 — Finance Transaction Package detail. Reads GET
// /transaction-packages/<id> (package info + documents by role + a
// read-only relationship_preview built from Phase 1's document_
// relationships, see helpers/transaction_packages.py::get_relationship_
// preview) — no calculation performed in the frontend.
@Component({
  selector: 'app-finance-transaction-detail',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './finance-transaction-detail.component.html',
  styleUrls: ['./finance-transaction-detail.component.css']
})
export class FinanceTransactionDetailComponent implements OnInit {
  packageId: number | null = null;
  package: any = null;
  documents: any = null;
  relationshipPreview: any[] = [];

  isLoading: boolean = false;
  errorMessage: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private route: ActivatedRoute,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    this.route.queryParams.subscribe(params => {
      const id = params['id'];
      if (id) {
        this.packageId = parseInt(id, 10);
        this.loadDetail();
      }
    });
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadDetail() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/transaction-packages/${this.packageId}`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.package = res.package;
        this.documents = res.documents;
        this.relationshipPreview = res.relationship_preview || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load transaction package.';
        this.cdr.detectChanges();
      }
    });
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

  formatAmount(amount: any, currency?: string | null): string {
    if (amount === null || amount === undefined || amount === '') return '-';
    return (currency || 'RM') + ' ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  goBack() {
    this.router.navigate(['/finance/transactions']);
  }
}
