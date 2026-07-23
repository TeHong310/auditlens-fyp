import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

@Component({
  selector: 'app-admin-documents',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-documents.component.html',
  styleUrls: ['./admin-documents.component.css']
})
export class AdminDocumentsComponent implements OnInit {
  documents: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';

  searchText: string = '';
  activeStatus: string = 'all';

  private apiUrl = environment.apiUrl;

  constructor(private http: HttpClient, private router: Router, private cdr: ChangeDetectorRef) {}

  ngOnInit() {
    this.loadDocuments();
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadDocuments() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/admin/documents`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.documents = res.documents || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load documents.';
        this.cdr.detectChanges();
      }
    });
  }

  get statusOptions(): string[] {
    const statuses = new Set(this.documents.map(d => d.status));
    return Array.from(statuses).sort();
  }

  countFor(status: string): number {
    return this.documents.filter(d => d.status === status).length;
  }

  setStatusFilter(status: string) {
    this.activeStatus = status;
  }

  get filteredDocuments(): any[] {
    let list = this.documents;
    if (this.activeStatus !== 'all') {
      list = list.filter(d => d.status === this.activeStatus);
    }
    const q = this.searchText.trim().toLowerCase();
    if (q) {
      list = list.filter(d =>
        (d.document_number || '').toLowerCase().includes(q) ||
        (d.vendor_name || '').toLowerCase().includes(q)
      );
    }
    return list;
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

  openRecord(doc: any) {
    this.router.navigate(['/admin/record-detail'], { queryParams: { document_id: doc.document_id } });
  }
}
