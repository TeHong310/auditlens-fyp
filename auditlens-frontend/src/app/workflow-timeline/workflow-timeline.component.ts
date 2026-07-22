import { Component, Input, OnChanges, SimpleChanges, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../environments/environment';

// Document Workflow Timeline — a reusable, read-only visualization
// component shown on both the Auditor Record Detail page and the
// Finance Correction Detail page. Purely a presentation layer: it
// fetches GET /documents/<id>/timeline (backend: routes/documents.py
// ::get_document_timeline/_build_timeline_events), which itself only
// reshapes data ALREADY computed elsewhere (three-way matching,
// authenticity, anomalies, review history, send-back cycles) — no new
// AI calls, no new tables, nothing here alters any of those engines.
@Component({
  selector: 'app-workflow-timeline',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './workflow-timeline.component.html',
  styleUrls: ['./workflow-timeline.component.css']
})
export class WorkflowTimelineComponent implements OnChanges {
  @Input() documentId: number | null = null;

  events: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnChanges(changes: SimpleChanges) {
    if (changes['documentId'] && this.documentId) {
      this.loadTimeline();
    }
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadTimeline() {
    if (!this.documentId) return;
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/documents/${this.documentId}/timeline`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.events = res.events || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load workflow timeline.';
        this.cdr.detectChanges();
      }
    });
  }

  // Exactly the 3 statuses the backend can send — completed / action_
  // required / pending — mapped to the icon set the feature specifies.
  statusIcon(status: string): string {
    if (status === 'completed') return 'ph-check-circle';
    if (status === 'action_required') return 'ph-warning';
    return 'ph-circle';
  }

  statusClass(status: string): string {
    if (status === 'completed') return 'wt-completed';
    if (status === 'action_required') return 'wt-action-required';
    return 'wt-pending';
  }

  formatDate(dateStr: string | null): string {
    if (!dateStr) return '';
    return new Date(dateStr).toLocaleString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'
    });
  }
}
