import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type RiskBucket = 'missing_docs' | 'high_risk' | 'passed' | 'pending';
type Filter = 'all' | 'pending' | 'high_risk' | 'missing_docs' | 'passed';

// Enterprise V3 Phase 13 — dedicated auditor Review Queue page (UI
// only). Reads the SAME GET /auditor/transactions endpoint the
// existing Auditor Home dashboard already uses (Phase 6) — no new
// backend route, no new matching/risk calculation. matching_status is
// the existing Enterprise Matching V2-derived verdict computed server-
// side; the "risk level" shown here is a pure client-side bucketing of
// that existing status plus the existing po_count/gr_count fields
// (missing documents outranks a matching review, which outranks a
// clean pass) — never a new assessment of the underlying documents.
@Component({
  selector: 'app-auditor-review-queue',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-review-queue.component.html',
  styleUrls: ['./auditor-review-queue.component.css']
})
export class AuditorReviewQueueComponent implements OnInit {

  isLoading: boolean = false;
  errorMessage: string = '';
  transactions: any[] = [];
  searchText: string = '';
  activeFilter: Filter = 'all';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadQueue();
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadQueue() {
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
        this.errorMessage = err.error?.error || 'Failed to load review queue.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Risk bucketing — pure presentation over already-computed fields,
  // never a new calculation. Missing documents outranks a matching
  // review (can't reliably assess a transaction that isn't complete
  // yet), which outranks a clean pass — same priority convention
  // routes/auditor.py's own _classify_exception() already uses
  // (mismatch/review outranks missing_document outranks a clean case). ──

  riskBucket(txn: any): RiskBucket {
    if (!txn.po_count || !txn.gr_count) return 'missing_docs';
    if (txn.matching_status === 'REVIEW') return 'high_risk';
    if (txn.matching_status === 'PASS') return 'passed';
    return 'pending';
  }

  riskLabel(txn: any): string {
    switch (this.riskBucket(txn)) {
      case 'missing_docs': return 'Medium Risk';
      case 'high_risk':    return 'High Risk';
      case 'passed':       return 'Low Risk';
      default:              return 'Medium Risk';
    }
  }

  riskClass(txn: any): string {
    switch (this.riskBucket(txn)) {
      case 'missing_docs': return 'risk-medium';
      case 'high_risk':    return 'risk-high';
      case 'passed':       return 'risk-low';
      default:              return 'risk-medium';
    }
  }

  get pendingCount(): number {
    return this.transactions.filter(t => this.riskBucket(t) === 'pending').length;
  }

  get highRiskCount(): number {
    return this.transactions.filter(t => this.riskBucket(t) === 'high_risk').length;
  }

  get missingDocsCount(): number {
    return this.transactions.filter(t => this.riskBucket(t) === 'missing_docs').length;
  }

  get completedCount(): number {
    return this.transactions.filter(t => this.riskBucket(t) === 'passed').length;
  }

  setFilter(f: Filter) {
    this.activeFilter = f;
  }

  get filteredTransactions() {
    let rows = this.transactions;
    if (this.activeFilter !== 'all') {
      rows = rows.filter(t => this.riskBucket(t) === this.activeFilter);
    }
    if (this.searchText.trim()) {
      const q = this.searchText.trim().toLowerCase();
      rows = rows.filter(t =>
        (t.package_name || '').toLowerCase().includes(q) ||
        (t.supplier || '').toLowerCase().includes(q)
      );
    }
    return rows;
  }

  // Identical navigation to the existing Auditor Home queue — clicking
  // Review opens the EXISTING, unmodified Record Detail page.
  goToRecord(txn: any) {
    if (txn.kind === 'transaction_package') {
      this.router.navigate(['/auditor/record-detail'], {
        queryParams: { document_id: txn.primary_document_id, transaction_package_id: txn.transaction_package_id }
      });
    } else {
      this.router.navigate(['/auditor/record-detail'], {
        queryParams: { document_id: txn.primary_document_id }
      });
    }
  }

  matchingStatusClass(status: string): string {
    switch (status) {
      case 'PASS':    return 'badge-approved';
      case 'REVIEW':  return 'badge-review';
      case 'PARTIAL': return 'badge-resubmitted';
      default:         return 'badge-pending';
    }
  }

  matchingStatusLabel(status: string): string {
    switch (status) {
      case 'PASS':    return 'PASS';
      case 'REVIEW':  return 'REVIEW REQUIRED';
      case 'PARTIAL': return 'PARTIAL';
      default:         return 'PENDING';
    }
  }
}
