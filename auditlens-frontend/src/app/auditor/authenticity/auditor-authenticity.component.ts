import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

type Filter = 'all' | 'passed' | 'warning';
type DocTypeFilter = 'all' | 'invoice' | 'po' | 'gr';

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
  activeDocTypeFilter: DocTypeFilter = 'all';

  // Enterprise V3 Phase 6 (STEP 8 + Additional Requirement) —
  // transaction-grouped authenticity view, additive to the existing
  // flat check list below (which now excludes any document already
  // shown inside a group, so nothing is double-listed). Reuses the
  // SAME GET /auditor/transactions + GET /auditor/transactions/<id>
  // endpoints Record Detail's Transaction Overview already calls — no
  // new backend route for this page. The authenticity ENGINE itself
  // (routes/authenticity.py, helpers/authenticity_check.py) is never
  // touched; this only groups/displays its existing output.
  transactionGroups: any[] = [];
  isLoadingGroups: boolean = false;
  expandedPackageIds: Set<number> = new Set();
  private groupedDocumentKeys: Set<string> = new Set();

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    this.loadTransactionGroups();
    this.loadChecks();
  }

  // ── Transaction-grouped authenticity (STEP 8 / Additional Requirement) ──

  loadTransactionGroups() {
    this.isLoadingGroups = true;
    this.http.get<any[]>(`${this.apiUrl}/auditor/transactions`, { headers: this.getHeaders() }).subscribe({
      next: (rows) => {
        const packageRows = (rows || []).filter(r => r.kind === 'transaction_package');
        if (packageRows.length === 0) {
          this.isLoadingGroups = false;
          this.cdr.detectChanges();
          return;
        }
        let remaining = packageRows.length;
        packageRows.forEach(row => {
          this.http.get<any>(`${this.apiUrl}/auditor/transactions/${row.transaction_package_id}`, { headers: this.getHeaders() }).subscribe({
            next: (detail) => {
              const auth = detail.authenticity_summary;
              this.transactionGroups.push({
                transaction_package_id: row.transaction_package_id,
                package_name: row.package_name,
                authenticity_summary: auth,
              });
              for (const roleKey of ['invoices', 'purchase_orders', 'goods_receipts']) {
                for (const doc of (auth?.documents?.[roleKey] || [])) {
                  this.groupedDocumentKeys.add(`${this.roleKeyToDocType(roleKey)}:${doc.document_id}`);
                }
              }
              remaining -= 1;
              if (remaining === 0) {
                this.isLoadingGroups = false;
                this.cdr.detectChanges();
              }
            },
            error: () => {
              remaining -= 1;
              if (remaining === 0) {
                this.isLoadingGroups = false;
                this.cdr.detectChanges();
              }
            }
          });
        });
      },
      error: () => { this.isLoadingGroups = false; }
    });
  }

  private roleKeyToDocType(roleKey: string): string {
    if (roleKey === 'invoices') return 'invoice';
    if (roleKey === 'purchase_orders') return 'po';
    return 'gr';
  }

  toggleGroup(packageId: number) {
    if (this.expandedPackageIds.has(packageId)) {
      this.expandedPackageIds.delete(packageId);
    } else {
      this.expandedPackageIds.add(packageId);
    }
  }

  isGroupExpanded(packageId: number): boolean {
    return this.expandedPackageIds.has(packageId);
  }

  groupDocumentRows(group: any): any[] {
    const auth = group.authenticity_summary;
    if (!auth?.documents) return [];
    return [
      ...(auth.documents.invoices || []).map((d: any) => ({ ...d, doc_type: 'invoice', label: d.invoice_number || d.file_name })),
      ...(auth.documents.purchase_orders || []).map((d: any) => ({ ...d, doc_type: 'po', label: d.po_number || d.file_name })),
      ...(auth.documents.goods_receipts || []).map((d: any) => ({ ...d, doc_type: 'gr', label: d.gr_number || d.file_name })),
    ];
  }

  groupStatusLabel(status: string | null): string {
    if (!status) return 'PENDING';
    return status === 'passed' ? 'PASS' : 'REVIEW';
  }

  groupStatusClass(status: string | null): string {
    if (!status) return 'group-status-pending';
    return status === 'passed' ? 'group-status-pass' : 'group-status-review';
  }

  viewGroupDocument(doc: any) {
    this.router.navigate(['/auditor/authenticity', doc.document_id], { queryParams: { document_type: doc.doc_type } });
  }

  // Every document already shown inside a transaction group is
  // excluded from the flat list below — the flat list is now ONLY the
  // STEP 10 backward-compatibility fallback for standalone documents.
  get ungroupedChecks() {
    return this.checks.filter(c => !this.groupedDocumentKeys.has(`${c.document_type}:${c.document_id}`));
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

  setDocTypeFilter(f: DocTypeFilter) {
    this.activeDocTypeFilter = f;
  }

  // Status filter and document-type filter combine with AND — e.g.
  // Invoice + Passed shows only passed invoices. Client-side only, over
  // the already-loaded list (no pagination on this endpoint currently).
  // Enterprise V3 Phase 6: filters over ungroupedChecks now (documents
  // already shown inside a transaction group above are excluded — this
  // section is the STEP 10 backward-compatibility fallback).
  get filteredChecks() {
    return this.ungroupedChecks.filter(c =>
      (this.activeFilter === 'all' || c.authenticity_status === this.activeFilter) &&
      (this.activeDocTypeFilter === 'all' || c.document_type === this.activeDocTypeFilter)
    );
  }

  // Each chip's count reflects only its own dimension (all documents
  // matching that status/type), same convention as the existing
  // All/Passed/Warning chips — independent of whatever else is
  // currently selected in the other filter row.
  get passedCount(): number {
    return this.ungroupedChecks.filter(c => c.authenticity_status === 'passed').length;
  }

  get warningCount(): number {
    return this.ungroupedChecks.filter(c => c.authenticity_status === 'warning').length;
  }

  get invoiceCount(): number {
    return this.ungroupedChecks.filter(c => c.document_type === 'invoice').length;
  }

  get poCount(): number {
    return this.ungroupedChecks.filter(c => c.document_type === 'po').length;
  }

  get grCount(): number {
    return this.ungroupedChecks.filter(c => c.document_type === 'gr').length;
  }

  viewDocument(check: any) {
    this.router.navigate(['/auditor/authenticity', check.document_id], {
      queryParams: { document_type: check.document_type }
    });
  }

  docTypeLabel(type: string): string {
    if (type === 'invoice') return 'Invoice';
    if (type === 'po') return 'PO';
    if (type === 'gr') return 'GR';
    return type || 'Unknown';
  }

  uploadSourceIcon(source: string): string {
    if (source === 'phone_photo') return 'ph-device-mobile-camera';
    if (source === 'scanned') return 'ph-printer';
    if (source === 'digital_native') return 'ph-desktop';
    if (source === 'webcam') return 'ph-webcam';
    return 'ph-question';
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
