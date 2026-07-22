import { Component, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';

type PackageRole = 'invoice' | 'purchase_order' | 'goods_receipt';

interface ProgressItem {
  name: string;
  role: PackageRole;
  status: 'pending' | 'uploading' | 'done' | 'error' | 'staged';
  message: string;
}

const ALLOWED_EXTENSIONS = ['pdf', 'jpg', 'jpeg', 'png'];

// Enterprise V3 Phase 5 — Finance Transaction Package creation (Mode 2
// of the upload experience; Mode 1, the existing single-document
// upload at /finance/upload, is completely untouched by this
// component). Orchestrates the EXISTING, unmodified upload endpoints
// (POST /documents/upload, /documents/upload-po/<id>, /documents/
// upload-gr/<id>) in sequence from the frontend, then links each
// resulting document into the new package via POST /transaction-
// packages/<id>/documents — no OCR/extraction/matching logic lives
// here, only orchestration and grouping.
@Component({
  selector: 'app-finance-transaction-create',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './finance-transaction-create.component.html',
  styleUrls: ['./finance-transaction-create.component.css']
})
export class FinanceTransactionCreateComponent {
  packageName: string = '';

  invoiceFiles: File[] = [];
  poFiles: File[] = [];
  grFiles: File[] = [];

  isSubmitting: boolean = false;
  progress: ProgressItem[] = [];
  errorMessage: string = '';
  successMessage: string = '';
  poGrStagedNotice: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) { }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── File selection (validation, multi-select, remove-before-upload) ──

  onFilesSelected(event: any, role: PackageRole) {
    const files: FileList = event.target.files;
    if (!files || files.length === 0) return;

    this.errorMessage = '';
    const invalid: string[] = [];
    const valid: File[] = [];
    for (const file of Array.from(files)) {
      const ext = file.name.split('.').pop()?.toLowerCase();
      if (!ext || !ALLOWED_EXTENSIONS.includes(ext)) {
        invalid.push(file.name);
      } else {
        valid.push(file);
      }
    }
    if (invalid.length > 0) {
      this.errorMessage = `File type not allowed: ${invalid.join(', ')}. Use: PDF, JPG, JPEG, PNG`;
    }

    this.targetArray(role).push(...valid);
    event.target.value = '';
    this.cdr.detectChanges();
  }

  removeFile(role: PackageRole, index: number) {
    this.targetArray(role).splice(index, 1);
    this.cdr.detectChanges();
  }

  private targetArray(role: PackageRole): File[] {
    if (role === 'invoice') return this.invoiceFiles;
    if (role === 'purchase_order') return this.poFiles;
    return this.grFiles;
  }

  get totalFileCount(): number {
    return this.invoiceFiles.length + this.poFiles.length + this.grFiles.length;
  }

  // ── Create Package: orchestrates the EXISTING upload endpoints ──

  async createPackage() {
    this.errorMessage = '';
    this.successMessage = '';
    this.poGrStagedNotice = '';

    if (!this.packageName.trim()) {
      this.errorMessage = 'Package name is required.';
      return;
    }
    if (this.totalFileCount === 0) {
      this.errorMessage = 'Select at least one file before creating a package.';
      return;
    }

    this.isSubmitting = true;
    this.progress = [
      ...this.invoiceFiles.map(f => ({ name: f.name, role: 'invoice' as PackageRole, status: 'pending' as const, message: '' })),
      ...this.poFiles.map(f => ({ name: f.name, role: 'purchase_order' as PackageRole, status: 'pending' as const, message: '' })),
      ...this.grFiles.map(f => ({ name: f.name, role: 'goods_receipt' as PackageRole, status: 'pending' as const, message: '' })),
    ];
    this.cdr.detectChanges();

    try {
      const pkg = await firstValueFrom(this.http.post<any>(
        `${this.apiUrl}/transaction-packages`, { package_name: this.packageName.trim() },
        { headers: this.getHeaders() }
      ));
      const packageId = pkg.id;

      let anchorDocumentId: number | null = null;

      for (const file of this.invoiceFiles) {
        const documentId = await this.uploadAndLink(packageId, file, 'invoice');
        if (documentId && anchorDocumentId === null) anchorDocumentId = documentId;
      }

      if (anchorDocumentId !== null) {
        for (const file of this.poFiles) {
          await this.uploadAndLink(packageId, file, 'purchase_order', anchorDocumentId);
        }
        for (const file of this.grFiles) {
          await this.uploadAndLink(packageId, file, 'goods_receipt', anchorDocumentId);
        }
      } else if (this.poFiles.length > 0 || this.grFiles.length > 0) {
        // No invoice was uploaded — Purchase Orders/Goods Receipts
        // cannot be OCR-processed without an invoice to anchor them
        // (the existing upload-po/upload-gr endpoints require one).
        // The package itself is still created (status stays
        // 'waiting_documents'); these files are marked staged, not
        // uploaded, and can be added once an invoice exists.
        for (const item of this.progress) {
          if (item.role !== 'invoice' && item.status === 'pending') {
            item.status = 'staged';
            item.message = 'Add an invoice to this package to process this file';
          }
        }
        this.poGrStagedNotice = 'Purchase Orders and Goods Receipts require at least one Invoice before they can be processed. ' +
          'This package was created and saved as "waiting_documents" — add an Invoice to continue.';
      }

      this.isSubmitting = false;
      const hasError = this.progress.some(p => p.status === 'error');
      this.successMessage = hasError
        ? 'Package created, but some files failed — see details below.'
        : 'Transaction package created successfully.';
      this.cdr.detectChanges();

      if (!hasError) {
        setTimeout(() => this.router.navigate(['/finance/transactions/detail'], { queryParams: { id: packageId } }), 1200);
      }
    } catch (err: any) {
      this.isSubmitting = false;
      this.errorMessage = err.error?.error || 'Failed to create transaction package.';
      this.cdr.detectChanges();
    }
  }

  // Uploads one file via the EXISTING per-role endpoint, then links the
  // resulting document into the package. Returns the new document_id
  // (or PO/GR id) on success, null on failure — never throws, so one
  // failed file doesn't stop the rest of the batch.
  private async uploadAndLink(packageId: number, file: File, role: PackageRole, anchorDocumentId?: number): Promise<number | null> {
    const item = this.progress.find(p => p.name === file.name && p.role === role)!;
    item.status = 'uploading';
    this.cdr.detectChanges();

    try {
      const formData = new FormData();
      formData.append('document', file);
      if (role === 'invoice') formData.append('input_method', 'upload');

      const uploadUrl = role === 'invoice'
        ? `${this.apiUrl}/documents/upload`
        : role === 'purchase_order'
          ? `${this.apiUrl}/documents/upload-po/${anchorDocumentId}`
          : `${this.apiUrl}/documents/upload-gr/${anchorDocumentId}`;

      const uploadRes = await firstValueFrom(this.http.post<any>(uploadUrl, formData, { headers: this.getHeaders() }));
      const documentId = role === 'invoice' ? uploadRes.document_id : (role === 'purchase_order' ? uploadRes.po_id : uploadRes.gr_id);

      await firstValueFrom(this.http.post<any>(
        `${this.apiUrl}/transaction-packages/${packageId}/documents`,
        { document_id: documentId, document_role: role },
        { headers: this.getHeaders() }
      ));

      item.status = 'done';
      item.message = 'Uploaded and linked';
      this.cdr.detectChanges();
      return documentId;
    } catch (err: any) {
      item.status = 'error';
      item.message = err.error?.error || 'Upload failed';
      this.cdr.detectChanges();
      return null;
    }
  }
}
