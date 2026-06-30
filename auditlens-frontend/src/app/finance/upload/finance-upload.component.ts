import { Component, OnInit, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

@Component({
  selector: 'app-finance-upload',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './finance-upload.component.html',
  styleUrls: ['./finance-upload.component.css']
})
export class FinanceUploadComponent implements OnInit {
  @ViewChild('fileInput') fileInputRef!: ElementRef;
  @ViewChild('poInput') poInputRef!: ElementRef;
  @ViewChild('grInput') grInputRef!: ElementRef;

  documents: any[] = [];
  isLoading: boolean = false;
  isUploading: boolean = false;
  errorMessage: string = '';
  successMessage: string = '';
  isDragOver: boolean = false;

  uploadQueue: { file: any, status: 'pending' | 'uploading' | 'done' | 'error', message: string }[] = [];

  // PO + GR
  selectedDocumentId: number | null = null;
  selectedDocumentName: string = '';
  isUploadingPO: boolean = false;
  isUploadingGR: boolean = false;
  poMessage: string = '';
  grMessage: string = '';
  poSuccess: boolean = false;
  grSuccess: boolean = false;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  ngOnInit() {
    this.loadQueueFromStorage();
    this.loadDocuments();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadDocuments() {
    this.isLoading = true;
    this.http.get<any>(`${this.apiUrl}/documents/`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.documents = res.documents;
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: () => { this.isLoading = false; }
    });
  }

  onBrowseFiles() {
    this.fileInputRef.nativeElement.click();
  }

  onFileSelected(event: any) {
    const files: FileList = event.target.files;
    if (files && files.length > 0) {
      this.handleFiles(Array.from(files));
    }
    event.target.value = '';
  }

  onDragOver(event: DragEvent) {
    event.preventDefault();
    this.isDragOver = true;
  }

  onDragLeave(event: DragEvent) {
    this.isDragOver = false;
  }

  onDrop(event: DragEvent) {
    event.preventDefault();
    this.isDragOver = false;
    const files = event.dataTransfer?.files;
    if (files && files.length > 0) {
      this.handleFiles(Array.from(files));
    }
  }

  handleFiles(files: File[]) {
    this.errorMessage = '';
    this.successMessage = '';

    const allowed = ['pdf', 'jpg', 'jpeg', 'png'];
    const validFiles: File[] = [];
    const invalidFiles: string[] = [];

    for (const file of files) {
      const ext = file.name.split('.').pop()?.toLowerCase();
      if (!ext || !allowed.includes(ext)) {
        invalidFiles.push(file.name);
      } else {
        validFiles.push(file);
      }
    }

    if (invalidFiles.length > 0) {
      this.errorMessage = `File type not allowed: ${invalidFiles.join(', ')}. Use: PDF, JPG, JPEG, PNG`;
    }

    if (validFiles.length === 0) return;

    const newFileNames = validFiles.map(f => f.name);
    this.uploadQueue = this.uploadQueue.filter(item =>
      !(newFileNames.includes(item.file.name) && item.status === 'error')
    );

    const newItems = validFiles.map(file => ({
      file,
      status: 'pending' as const,
      message: ''
    }));

    this.uploadQueue = [...this.uploadQueue, ...newItems];
    this.saveQueueToStorage();
    this.cdr.detectChanges();
    this.uploadNextInQueue();
  }

  uploadNextInQueue() {
    const pendingIndex = this.uploadQueue.findIndex(item => item.status === 'pending');
    if (pendingIndex === -1) {
      this.isUploading = false;
      this.loadDocuments();
      this.saveQueueToStorage();

      const hasError = this.uploadQueue.some(item => item.status === 'error');
      if (hasError) {
        this.successMessage = 'Some files failed. Click Retry Failed to re-upload.';
      } else {
        this.successMessage = 'All files uploaded successfully!';
      }
      this.cdr.detectChanges();
      setTimeout(() => {
        this.successMessage = '';
        this.cdr.detectChanges();
      }, 5000);
      return;
    }

    this.isUploading = true;
    this.uploadQueue[pendingIndex].status = 'uploading';
    this.saveQueueToStorage();
    this.cdr.detectChanges();

    const item = this.uploadQueue[pendingIndex];

    if (!item.file || !(item.file instanceof File)) {
      this.uploadQueue[pendingIndex].status = 'error';
      this.uploadQueue[pendingIndex].message = 'Please re-select this file';
      this.saveQueueToStorage();
      this.cdr.detectChanges();
      this.uploadNextInQueue();
      return;
    }

    const file = item.file;
    const formData = new FormData();
    formData.append('document', file);
    formData.append('input_method', 'upload');

    const token = localStorage.getItem('access_token');

    this.http.post<any>(`${this.apiUrl}/documents/upload`, formData, {
      headers: new HttpHeaders({ 'Authorization': `Bearer ${token}` })
    }).subscribe({
      next: (res) => {
        this.uploadQueue[pendingIndex].status = 'done';
        this.uploadQueue[pendingIndex].message = 'Uploaded successfully';
        this.saveQueueToStorage();
        this.cdr.detectChanges();
        this.uploadNextInQueue();
      },
      error: (err) => {
        this.uploadQueue[pendingIndex].status = 'error';
        this.uploadQueue[pendingIndex].message = err.error?.error || 'Upload failed';
        this.saveQueueToStorage();
        this.cdr.detectChanges();
        this.uploadNextInQueue();
      }
    });
  }

  // ── PO + GR ──────────────────────────────────────────────

  selectDocumentForSupporting(doc: any) {
    this.selectedDocumentId = doc.document_id;
    this.selectedDocumentName = doc.file_name;
    this.poMessage = '';
    this.grMessage = '';
    this.poSuccess = false;
    this.grSuccess = false;
    this.cdr.detectChanges();

    // Scroll to supporting section
    setTimeout(() => {
      const el = document.getElementById('supporting-section');
      if (el) el.scrollIntoView({ behavior: 'smooth' });
    }, 100);
  }

  onPOFileSelected(event: any) {
    const file = event.target.files[0];
    if (!file || !this.selectedDocumentId) return;
    this.uploadPO(file);
    event.target.value = '';
  }

  onGRFileSelected(event: any) {
    const file = event.target.files[0];
    if (!file || !this.selectedDocumentId) return;
    this.uploadGR(file);
    event.target.value = '';
  }

  uploadPO(file: File) {
    this.isUploadingPO = true;
    this.poMessage = '';
    this.poSuccess = false;

    const formData = new FormData();
    formData.append('document', file);
    const token = localStorage.getItem('access_token');

    this.http.post<any>(
      `${this.apiUrl}/documents/upload-po/${this.selectedDocumentId}`,
      formData,
      { headers: new HttpHeaders({ 'Authorization': `Bearer ${token}` }) }
    ).subscribe({
      next: (res) => {
        this.isUploadingPO = false;
        this.poSuccess = true;
        this.poMessage = `PO uploaded! PO Number: ${res.extracted_fields?.po_number || 'N/A'}`;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isUploadingPO = false;
        this.poSuccess = false;
        this.poMessage = err.error?.error || 'PO upload failed';
        this.cdr.detectChanges();
      }
    });
  }

  uploadGR(file: File) {
    this.isUploadingGR = true;
    this.grMessage = '';
    this.grSuccess = false;

    const formData = new FormData();
    formData.append('document', file);
    const token = localStorage.getItem('access_token');

    this.http.post<any>(
      `${this.apiUrl}/documents/upload-gr/${this.selectedDocumentId}`,
      formData,
      { headers: new HttpHeaders({ 'Authorization': `Bearer ${token}` }) }
    ).subscribe({
      next: (res) => {
        this.isUploadingGR = false;
        this.grSuccess = true;
        this.grMessage = `GR uploaded! GR Number: ${res.extracted_fields?.gr_number || 'N/A'}`;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isUploadingGR = false;
        this.grSuccess = false;
        this.grMessage = err.error?.error || 'GR upload failed';
        this.cdr.detectChanges();
      }
    });
  }

  // ── existing functions below (unchanged) ─────────────────

  retryFailed() {
    let hasValidRetry = false;
    this.uploadQueue = this.uploadQueue.map(item => {
      if (item.status === 'error' && item.file instanceof File) {
        hasValidRetry = true;
        return { ...item, status: 'pending' as const, message: '' };
      }
      return item;
    });
    this.saveQueueToStorage();
    this.cdr.detectChanges();
    if (hasValidRetry) {
      this.uploadNextInQueue();
    } else {
      this.errorMessage = 'No valid files to retry. Please re-select the failed files.';
      setTimeout(() => { this.errorMessage = ''; this.cdr.detectChanges(); }, 4000);
    }
  }

  viewDocument(doc: any) {
    const token = localStorage.getItem('access_token');
    const url = `${this.apiUrl}/documents/${doc.document_id}/file`;
    fetch(url, { headers: { 'Authorization': `Bearer ${token}` } })
      .then(res => { if (!res.ok) throw new Error('Failed'); return res.blob(); })
      .then(blob => window.open(URL.createObjectURL(blob), '_blank'))
      .catch(() => { this.errorMessage = 'Failed to open file.'; this.cdr.detectChanges(); });
  }

  saveQueueToStorage() {
    const simplified = this.uploadQueue.map(item => ({
      name: item.file.name, size: item.file.size,
      status: item.status, message: item.message
    }));
    localStorage.setItem('uploadQueue', JSON.stringify(simplified));
  }

  loadQueueFromStorage() {
    const saved = localStorage.getItem('uploadQueue');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        this.uploadQueue = parsed.map((item: any) => ({
          file: { name: item.name, size: item.size } as File,
          status: (item.status === 'uploading' || item.status === 'pending') ? 'error' : item.status,
          message: (item.status === 'uploading' || item.status === 'pending') ? 'Please re-select this file' : item.message
        }));
      } catch (e) { localStorage.removeItem('uploadQueue'); }
    }
  }

  hasRetryableErrors(): boolean {
    return this.uploadQueue.some(item => item.status === 'error' && item.file instanceof File);
  }

  clearDoneItems() {
    this.uploadQueue = this.uploadQueue.filter(item => item.status !== 'done');
    this.saveQueueToStorage();
    this.cdr.detectChanges();
  }

  clearQueue() {
    this.uploadQueue = [];
    localStorage.removeItem('uploadQueue');
    this.cdr.detectChanges();
  }

  getStatusClass(status: string): string {
    switch (status) {
      case 'ocr_done': return 'badge-processed';
      case 'under_review': return 'badge-review';
      case 'approved': return 'badge-matched';
      case 'returned': return 'badge-returned';
      default: return 'badge-pending';
    }
  }

  currentPage: number = 1;
  pageSize: number = 5;

  get paginatedDocuments() {
    const start = (this.currentPage - 1) * this.pageSize;
    return this.documents.slice(start, start + this.pageSize);
  }

  get totalPages(): number {
    return Math.ceil(this.documents.length / this.pageSize);
  }

  get pageNumbers(): number[] {
    return Array.from({ length: this.totalPages }, (_, i) => i + 1);
  }

  goToPage(page: number) {
    if (page >= 1 && page <= this.totalPages) this.currentPage = page;
  }
  deleteDocument(doc: any) {
  if (!confirm(`Delete "${doc.file_name}"? This cannot be undone.`)) return;

  const token = localStorage.getItem('access_token');
  this.http.delete<any>(`${this.apiUrl}/documents/${doc.document_id}`, {
    headers: new HttpHeaders({ 'Authorization': `Bearer ${token}` })
  }).subscribe({
    next: () => {
      this.documents = this.documents.filter(d => d.document_id !== doc.document_id);
      this.cdr.detectChanges();
    },
    error: (err) => {
      this.errorMessage = err.error?.error || 'Failed to delete.';
      this.cdr.detectChanges();
    }
  });
}

  getStatusLabel(status: string): string {
    switch (status) {
      case 'ocr_done': return 'Processed';
      case 'under_review': return 'Under Review';
      case 'approved': return 'Approved';
      case 'returned': return 'Returned';
      case 'ocr_processing': return 'Processing...';
      default: return status;
    }
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }
}