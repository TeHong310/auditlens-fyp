import { Component, OnInit, OnDestroy, ElementRef, ViewChild, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders, HttpEventType, HttpEvent } from '@angular/common/http';
import { forkJoin, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import JSZip from 'jszip';
import { environment } from '../../../environments/environment';
import { FinanceUserMenuComponent } from '../shared/finance-user-menu.component';

// Same "pick the most urgent member" ranking finance-home.component.ts
// ::computeQueueGroups() already uses to choose a group's primary doc
// (status/vendor display, View/Attach/Delete targeting) — copied
// unchanged.
const STATUS_PRIORITY: Record<string, number> = {
  returned: 5, under_review: 4, resubmitted: 4, ocr_processing: 3, ocr_done: 2, approved: 1,
};

// The 3 document types this page recognizes from a filename — kept
// separate from any backend document_type enum (e.g. authenticity_
// checks.document_type's 'po'/'gr') per the task's own "without
// changing the underlying backend document-type values" requirement.
// This value only ever decides WHICH existing endpoint a queue item is
// sent to (see uploadNextInQueue()) — it is never sent to the backend
// as a form field.
export type DocType = 'invoice' | 'purchase_order' | 'goods_receipt';

// A staged file whose filename matched none of the known type patterns
// — never silently uploaded as any of the 3 real types; see
// confirmStaged()/confirmAllStaged() for the gate that keeps it out of
// uploadQueue until the user picks one.
export type StagedDocType = DocType | 'unconfirmed';

// Upload-processing order within a batch: Invoice must always be
// attempted before its own Purchase Order/Goods Receipt (which attach
// TO the invoice's document_id) — never the order files happened to
// appear in a ZIP archive. See uploadNextInQueue()'s pending-item
// selection below, the one place this is actually enforced for the
// real API calls.
const DOC_TYPE_UPLOAD_PRIORITY: Record<DocType, number> = {
  invoice: 0,
  purchase_order: 1,
  goods_receipt: 2,
};

export type QueueStatus = 'pending' | 'uploading' | 'processing' | 'done' | 'error';

export interface QueueItem {
  file: File;
  docType: DocType;
  status: QueueStatus;
  message: string;
  previewUrl?: string;
  progress: number;       // 0-100, real upload % — only meaningful while status === 'uploading'
  batchId: number;        // groups files added together in one handleFiles() call — used to
                           // find "the invoice from THIS batch" for purchase_order/goods_receipt routing
}

// A file that has been selected/extracted but not yet confirmed into
// the actual upload queue — see the task's "Do not upload a file until
// its document type is confirmed" requirement.
export interface StagedFile {
  id: number;
  file: File;
  docType: StagedDocType;
  inferredType: StagedDocType;
  previewUrl?: string;
  batchId: number;
}

const ALLOWED_EXTENSIONS = ['pdf', 'jpg', 'jpeg', 'png'];
// Matches the ONLY existing size figure documented anywhere on this
// page (the "Upload Rules" card's "Maximum 10MB per file") — there is
// no enforced hard limit today for manually-selected files, so this is
// reused specifically for ZIP-extracted entries per the task's own
// "Reuse the project's existing file-size limits" instruction, without
// retroactively changing the (currently unenforced) manual-select path.
const MAX_ZIP_ENTRY_BYTES = 10 * 1024 * 1024;

const MIME_BY_EXT: Record<string, string> = {
  pdf: 'application/pdf',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  png: 'image/png',
};

@Component({
  selector: 'app-finance-upload',
  standalone: true,
  imports: [CommonModule, FinanceUserMenuComponent],
  templateUrl: './finance-upload.component.html',
  styleUrls: ['./finance-upload.component.css']
})
export class FinanceUploadComponent implements OnInit, OnDestroy {
  @ViewChild('fileInput') fileInputRef!: ElementRef;
  @ViewChild('poInput') poInputRef!: ElementRef;
  @ViewChild('grInput') grInputRef!: ElementRef;

  // "Recent Transaction Packages" — one row per real transaction
  // package (or standalone invoice), built by computeGroupedRows()
  // below once documents/PO+GR lists/packages have all loaded. This is
  // the table/pagination-facing array; the underlying per-invoice
  // records live in invoiceRecords.
  documents: any[] = [];
  private invoiceRecords: any[] = [];
  private documentsLoaded: boolean = false;

  // Package grouping — reuses the EXACT SAME data sources and shapes
  // finance-home.component.ts's own Document Processing Queue groups
  // by: GET /documents/po/list + GET /documents/gr/list (for standalone
  // invoices' own directly-linked PO/GR) and GET /transaction-packages
  // + GET /transaction-packages/<id> (for real transaction_package_id
  // membership) — not document_id, vendor name, or guessed PO numbers.
  // No new endpoint, no new grouping logic: copied unchanged from
  // finance-home.component.ts.
  private poList: any[] = [];
  private grList: any[] = [];
  private poByDocId: Map<number, string> = new Map();
  private grNumbersByDocId: Map<number, string[]> = new Map();
  private poGrLoaded: boolean = false;
  private packageGroupByDocId: Map<number, { packageId: number; invoiceNumbers: string[]; poNumbers: string[]; grNumbers: string[] }> = new Map();
  private packagesLoaded: boolean = false;

  isLoading: boolean = false;
  isUploading: boolean = false;
  errorMessage: string = '';
  successMessage: string = '';
  isDragOver: boolean = false;

  uploadQueue: QueueItem[] = [];

  // Files awaiting document-type confirmation — populated by
  // handleFiles() (manual select, drag-drop, or ZIP extraction) and
  // moved into uploadQueue only once confirmed (confirmStaged()/
  // confirmAllStaged()).
  stagedFiles: StagedFile[] = [];
  private stagedIdCounter = 0;
  private batchCounter = 0;
  // The document_id of the most recently successfully-uploaded Invoice
  // per batch — purchase_order/goods_receipt queue items from the SAME
  // batch attach to this via the existing upload-po/upload-gr endpoints
  // (falls back to selectedDocumentId, the page's existing "Attach
  // PO/GR" target, if no invoice from the same batch is available yet).
  private batchAnchorInvoice: Record<number, number> = {};

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
    // Fired together, in parallel — whichever resolves last is the one
    // that actually produces the grouped rows (see computeGroupedRows()
    // 's own gate), same multi-source-load pattern finance-home.
    // component.ts uses for its own Document Processing Queue.
    this.loadDocuments();
    this.loadPoGrLists();
    this.loadTransactionPackages();
  }

  ngOnDestroy() {
    this.uploadQueue.forEach(item => {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    });
    this.stagedFiles.forEach(item => {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    });
  }

  isImageFile(file: any): boolean {
    const ext = file?.name?.split('.').pop()?.toLowerCase();
    return ext === 'jpg' || ext === 'jpeg' || ext === 'png';
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
        this.invoiceRecords = res.documents;
        this.documentsLoaded = true;
        this.computeGroupedRows();
      },
      error: () => { this.isLoading = false; }
    });
  }

  // Copied unchanged from finance-home.component.ts::loadPoGrLists() —
  // reuses the EXISTING GET /documents/po/list and GET /documents/gr/
  // list endpoints (already finance-scoped server-side) to know, per
  // standalone invoice, whether its own directly-linked PO/GR exists.
  loadPoGrLists() {
    forkJoin({
      po: this.http.get<any>(`${this.apiUrl}/documents/po/list`, { headers: this.getHeaders() })
        .pipe(catchError(() => of({ purchase_orders: [] }))),
      gr: this.http.get<any>(`${this.apiUrl}/documents/gr/list`, { headers: this.getHeaders() })
        .pipe(catchError(() => of({ goods_receipts: [] }))),
    }).subscribe(({ po, gr }) => {
      this.poList = po.purchase_orders || [];
      this.grList = gr.goods_receipts || [];
      this.poByDocId = new Map();
      for (const p of this.poList) {
        if (p.po_number) this.poByDocId.set(p.document_id, p.po_number);
      }
      this.grNumbersByDocId = new Map();
      for (const g of this.grList) {
        if (!g.gr_number) continue;
        const arr = this.grNumbersByDocId.get(g.document_id) || [];
        arr.push(g.gr_number);
        this.grNumbersByDocId.set(g.document_id, arr);
      }
      this.poGrLoaded = true;
      this.computeGroupedRows();
    });
  }

  // Copied unchanged from finance-home.component.ts::
  // loadTransactionPackages() — reuses the EXISTING GET /transaction-
  // packages (list) and GET /transaction-packages/<id> (detail, same
  // get_package_documents() helper backing it) endpoints. No new
  // backend endpoint, no transaction-package logic touched.
  loadTransactionPackages() {
    this.http.get<any[]>(`${this.apiUrl}/transaction-packages`, { headers: this.getHeaders() })
      .pipe(catchError(() => of([])))
      .subscribe((packages: any[]) => {
        if (!packages.length) {
          this.packagesLoaded = true;
          this.computeGroupedRows();
          return;
        }

        const requests: { [id: number]: any } = {};
        for (const pkg of packages) {
          requests[pkg.id] = this.http.get<any>(`${this.apiUrl}/transaction-packages/${pkg.id}`, { headers: this.getHeaders() })
            .pipe(catchError(() => of(null)));
        }

        forkJoin(requests).subscribe((results: any) => {
          const map = new Map<number, { packageId: number; invoiceNumbers: string[]; poNumbers: string[]; grNumbers: string[] }>();
          for (const pkg of packages) {
            const detail = results[pkg.id];
            const docs = detail?.documents;
            if (!docs) continue;

            const group = {
              packageId: pkg.id,
              invoiceNumbers: Array.from(new Set<string>(docs.invoices.map((d: any) => d.invoice_number).filter(Boolean))),
              poNumbers: Array.from(new Set<string>(docs.purchase_orders.map((p: any) => p.po_number).filter(Boolean))),
              grNumbers: Array.from(new Set<string>(docs.goods_receipts.map((g: any) => g.gr_number).filter(Boolean))),
            };
            for (const inv of docs.invoices) {
              map.set(inv.document_id, group);
            }
          }
          this.packageGroupByDocId = map;
          this.packagesLoaded = true;
          this.computeGroupedRows();
        });
      });
  }

  // Groups invoiceRecords by real transaction_package_id (via
  // packageGroupByDocId) into one row per package; an invoice not
  // linked into any package stays its own row, exactly like Finance
  // Home's own standalone rows. Needs all 3 loads done.
  private computeGroupedRows() {
    if (!this.documentsLoaded || !this.poGrLoaded || !this.packagesLoaded) return;

    const docsByPackageId = new Map<number, any[]>();
    const standaloneDocs: any[] = [];
    for (const doc of this.invoiceRecords) {
      const group = this.packageGroupByDocId.get(doc.document_id);
      if (group) {
        const arr = docsByPackageId.get(group.packageId) || [];
        arr.push(doc);
        docsByPackageId.set(group.packageId, arr);
      } else {
        standaloneDocs.push(doc);
      }
    }

    const rows: any[] = [];
    for (const docs of docsByPackageId.values()) {
      const group = this.packageGroupByDocId.get(docs[0].document_id)!;
      rows.push(this.buildRow(group.packageId, docs, group.poNumbers, group.grNumbers));
    }
    for (const doc of standaloneDocs) {
      const poNumber = this.poByDocId.get(doc.document_id);
      const poNumbers = poNumber ? [poNumber] : [];
      const grNumbers = this.grNumbersByDocId.get(doc.document_id) || [];
      rows.push(this.buildRow(null, [doc], poNumbers, grNumbers));
    }

    rows.sort((a, b) => new Date(b.uploadedAt || 0).getTime() - new Date(a.uploadedAt || 0).getTime());
    this.documents = rows;

    this.isLoading = false;
    this.cdr.detectChanges();
  }

  // One package's (or one standalone invoice's) aggregate row.
  // poNumbers/grNumbers are already deduped by the caller (package:
  // from packageGroupByDocId, itself Set-deduped at load time;
  // standalone: at most one PO / this invoice's own GR numbers).
  private buildRow(packageId: number | null, docs: any[], poNumbers: string[], grNumbers: string[]): any {
    const primary = [...docs].sort((a, b) => (STATUS_PRIORITY[b.status] || 0) - (STATUS_PRIORITY[a.status] || 0))[0];
    const newest = [...docs].sort((a, b) => new Date(b.uploaded_at || 0).getTime() - new Date(a.uploaded_at || 0).getTime())[0];

    const invoiceNumbers = Array.from(new Set(docs.map(d => d.invoice_number).filter(Boolean)));
    const missingDocs = poNumbers.length === 0 || grNumbers.length === 0;

    // Package Status — workflow state (Returned/Under Review/Approved-
    // only-if-complete) takes priority since it reflects actual auditor
    // engagement; Missing Documents only applies when no review has
    // started yet and a required PO/GR is genuinely absent; otherwise
    // Pending.
    let statusLabel: string;
    if (docs.some(d => d.status === 'returned')) statusLabel = 'Returned for Correction';
    else if (docs.some(d => d.status === 'under_review')) statusLabel = 'Under Review';
    else if (docs.every(d => d.status === 'approved')) statusLabel = 'Approved';
    else if (missingDocs) statusLabel = 'Missing Documents';
    else statusLabel = 'Pending';

    // Delete stays available only for an eligible draft/unprocessed
    // STANDALONE upload (still mid-OCR-pipeline, never reached the
    // audit workflow) — never for a formal transaction package,
    // regardless of status, and never once a standalone invoice has
    // reached under_review/approved/returned.
    const eligibleForDelete = packageId === null && (primary.status === 'ocr_processing' || primary.status === 'ocr_done');

    return {
      packageId,
      documentIds: docs.map(d => d.document_id),
      primaryDoc: primary,
      invoiceLabel: invoiceNumbers.length ? invoiceNumbers.join(', ') : (docs[0].file_name || '-'),
      relatedDocumentsLabel: this.formatRelatedDocuments(poNumbers, grNumbers),
      uploadedAt: newest.uploaded_at,
      statusLabel,
      statusClass: this.packageStatusClassFor(statusLabel),
      showAttach: missingDocs,
      eligibleForDelete,
    };
  }

  private formatRelatedDocuments(poNumbers: string[], grNumbers: string[]): string {
    const po = poNumbers.length ? `PO: ${poNumbers.join(', ')}` : 'PO: Not Uploaded';
    const gr = grNumbers.length ? `GR: ${grNumbers.join(', ')}` : 'GR: Not Uploaded';
    return `${po} · ${gr}`;
  }

  private packageStatusClassFor(label: string): string {
    if (label === 'Approved') return 'badge-matched';
    if (label === 'Returned for Correction') return 'badge-returned';
    if (label === 'Under Review') return 'badge-review';
    if (label === 'Missing Documents') return 'badge-returned';
    return 'badge-pending'; // Pending
  }

  // Action column wrappers — each extracts the right underlying raw
  // document from the grouped row and reuses the EXISTING single-
  // document handlers below unchanged.

  viewRow(row: any) {
    if (row.packageId) {
      this.router.navigate(['/finance/transactions/detail'], { queryParams: { id: row.packageId } });
    } else {
      this.viewDocument(row.primaryDoc);
    }
  }

  attachRow(row: any) {
    this.selectDocumentForSupporting(row.primaryDoc);
  }

  deleteRow(row: any) {
    this.deleteDocument(row.primaryDoc);
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

  // Entry point for every file-selection path (Browse Files, drag-drop,
  // and — via extractZipFile() below — a .zip's contents). ZIP files
  // are extracted client-side and never themselves added to the queue
  // or uploaded; their supported entries are merged in with any
  // directly-selected files and go through the SAME validation/dedup/
  // type-inference staging step before anything is queued for upload.
  async handleFiles(files: File[]) {
    this.errorMessage = '';
    this.successMessage = '';

    const batchId = ++this.batchCounter;
    const zipInputs = files.filter(f => this.isZipFile(f));
    const directInputs = files.filter(f => !this.isZipFile(f));

    const candidates: File[] = [...directInputs];
    const zipNotes: string[] = [];
    for (const zip of zipInputs) {
      const result = await this.extractZipFile(zip);
      candidates.push(...result.files);
      zipNotes.push(...result.notes);
    }

    const validFiles: File[] = [];
    const invalidFiles: string[] = [];
    for (const file of candidates) {
      const ext = file.name.split('.').pop()?.toLowerCase();
      if (!ext || !ALLOWED_EXTENSIONS.includes(ext)) {
        invalidFiles.push(file.name);
      } else {
        validFiles.push(file);
      }
    }

    // A same-name/same-size file already staged or actively queued
    // (pending/uploading/processing/done) is a true duplicate and is
    // skipped. A same-name/same-size 'error' queue item is treated as
    // replaceable — matches the existing retry-by-re-add behavior this
    // page already had before this change.
    const seenKeys = new Set<string>();
    this.stagedFiles.forEach(s => seenKeys.add(`${s.file.name}:${s.file.size}`));
    this.uploadQueue.forEach(q => { if (q.status !== 'error') seenKeys.add(`${q.file.name}:${q.file.size}`); });

    this.uploadQueue = this.uploadQueue.filter(item =>
      !(validFiles.some(f => f.name === item.file.name && f.size === item.file.size) && item.status === 'error')
    );

    const duplicateNames: string[] = [];
    const newStaged: StagedFile[] = [];
    for (const file of validFiles) {
      const key = `${file.name}:${file.size}`;
      if (seenKeys.has(key)) {
        duplicateNames.push(file.name);
        continue;
      }
      seenKeys.add(key);
      const inferred = this.inferDocType(file.name);
      newStaged.push({
        id: ++this.stagedIdCounter,
        file,
        docType: inferred,
        inferredType: inferred,
        previewUrl: this.isImageFile(file) ? URL.createObjectURL(file) : undefined,
        batchId,
      });
    }

    const messages: string[] = [];
    if (invalidFiles.length > 0) {
      messages.push(`File type not allowed: ${invalidFiles.join(', ')}. Use: PDF, JPG, JPEG, PNG`);
    }
    if (duplicateNames.length > 0) {
      messages.push(`Already in the queue, skipped: ${duplicateNames.join(', ')}`);
    }
    messages.push(...zipNotes);
    if (messages.length > 0) {
      this.errorMessage = messages.join(' ');
    }

    if (newStaged.length > 0) {
      this.stagedFiles = [...this.stagedFiles, ...newStaged];
    }
    this.cdr.detectChanges();
  }

  private isZipFile(file: File): boolean {
    return file.name.toLowerCase().endsWith('.zip');
  }

  // Extracts supported documents from a ZIP client-side (JSZip) — the
  // ZIP itself is never uploaded or stored. Directory entries, __MACOSX
  // metadata, and dotfiles (e.g. .DS_Store) are ignored silently as
  // routine ZIP noise; nested ZIPs, unreadable/encrypted entries, and
  // unsupported formats are skipped with a note so the user knows
  // something in their ZIP was excluded. A ZIP that is itself encrypted
  // fails to load at all and is reported as a single top-level note.
  private async extractZipFile(zipFile: File): Promise<{ files: File[]; notes: string[] }> {
    const notes: string[] = [];
    let zip: JSZip;
    try {
      zip = await JSZip.loadAsync(zipFile);
    } catch (e) {
      notes.push(`"${zipFile.name}" could not be read — it may be encrypted, password-protected, or corrupted.`);
      return { files: [], notes };
    }

    const extracted: File[] = [];

    for (const relativePath of Object.keys(zip.files)) {
      const entry = zip.files[relativePath];
      if (entry.dir) continue;

      const segments = relativePath.split('/');
      const baseName = segments[segments.length - 1];
      if (!baseName) continue; // stray trailing-slash-only entries

      if (segments.includes('__MACOSX')) continue;
      if (baseName === '.DS_Store') continue;
      if (baseName.startsWith('.')) continue;

      const ext = baseName.split('.').pop()?.toLowerCase() || '';

      if (ext === 'zip') {
        notes.push(`${baseName}: nested ZIP files are not supported, skipped.`);
        continue;
      }
      if (!ALLOWED_EXTENSIONS.includes(ext)) {
        notes.push(`${baseName}: unsupported file type, skipped.`);
        continue;
      }

      let bytes: Uint8Array;
      try {
        bytes = await entry.async('uint8array');
      } catch (e) {
        notes.push(`${baseName}: could not be extracted (likely encrypted), skipped.`);
        continue;
      }

      if (bytes.length > MAX_ZIP_ENTRY_BYTES) {
        notes.push(`${baseName}: exceeds the 10MB size limit, skipped.`);
        continue;
      }

      const mime = MIME_BY_EXT[ext] || 'application/octet-stream';
      extracted.push(new File([bytes as BlobPart], baseName, { type: mime }));
    }

    if (extracted.length === 0) {
      notes.unshift(`"${zipFile.name}" contains no supported documents (PDF, JPG, JPEG, PNG).`);
    }

    return { files: extracted, notes };
  }

  // Filename-based document-type guess — an editable starting point
  // only; see confirmStaged()/confirmAllStaged() for the confirmation
  // gate that must happen before any file actually uploads.
  //
  // Detection priority is 1) Goods Receipt/GRPO, 2) Purchase Order/PO,
  // 3) Invoice/INV — checked in that order specifically so a compound
  // token like "GRPO" (Goods Receipt PO) is matched as Goods Receipt
  // BEFORE the Purchase Order check ever runs, never as a bare "PO".
  // A filename matching none of the 3 is left 'unconfirmed' rather than
  // defaulting to Invoice — the user must pick a real type in "Confirm
  // Document Types" before it can be queued.
  inferDocType(fileName: string): StagedDocType {
    const name = fileName.toLowerCase();

    // Goods Receipt: GR, GRPO, GRN, "Goods Receipt", "Goods Receipt PO",
    // "Goods Received Note".
    if (/(^|[\s_.\-])(goods[\s_\-]?receipt(?:[\s_\-]?po)?|goods[\s_\-]?received[\s_\-]?note|grpo|grn|gr)([\s_.\-]|$)/.test(name)) {
      return 'goods_receipt';
    }
    // Purchase Order: PO, "Purchase Order".
    if (/(^|[\s_.\-])(purchase[\s_\-]?order|po)([\s_.\-]|$)/.test(name)) {
      return 'purchase_order';
    }
    // Invoice: INV, "Invoice".
    if (/(^|[\s_.\-])(invoice|inv)([\s_.\-]|$)/.test(name)) {
      return 'invoice';
    }
    return 'unconfirmed';
  }

  // ── Type-confirmation staging (gates entry into uploadQueue) ──────

  updateStagedType(item: StagedFile, type: DocType) {
    item.docType = type;
  }

  removeStaged(item: StagedFile) {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    this.stagedFiles = this.stagedFiles.filter(s => s.id !== item.id);
    this.cdr.detectChanges();
  }

  confirmStaged(item: StagedFile) {
    // A file whose type couldn't be inferred must be classified by the
    // user first — never silently queued as any particular type.
    if (item.docType === 'unconfirmed') return;
    this.stagedFiles = this.stagedFiles.filter(s => s.id !== item.id);
    this.uploadQueue = [...this.uploadQueue, this.toQueueItem(item)];
    this.saveQueueToStorage();
    this.cdr.detectChanges();
    this.uploadNextInQueue();
  }

  confirmAllStaged() {
    if (this.stagedFiles.length === 0) return;
    this.errorMessage = '';

    // Only files with a real, confirmed type are queued; anything still
    // 'unconfirmed' stays staged until the user picks a type for it.
    const confirmable = this.stagedFiles.filter(item => item.docType !== 'unconfirmed');
    const remaining = this.stagedFiles.filter(item => item.docType === 'unconfirmed');

    if (confirmable.length === 0) {
      this.errorMessage = 'Select a document type for each file before confirming.';
      this.cdr.detectChanges();
      return;
    }

    const newItems = confirmable.map(item => this.toQueueItem(item));
    this.stagedFiles = remaining;
    this.uploadQueue = [...this.uploadQueue, ...newItems];
    if (remaining.length > 0) {
      this.errorMessage = 'Select a document type for each remaining file before confirming.';
    }
    this.saveQueueToStorage();
    this.cdr.detectChanges();
    this.uploadNextInQueue();
  }

  private toQueueItem(item: StagedFile): QueueItem {
    return {
      file: item.file,
      docType: item.docType as DocType, // callers only ever pass a confirmed (non-'unconfirmed') item
      status: 'pending',
      message: '',
      previewUrl: item.previewUrl,
      progress: 0,
      batchId: item.batchId,
    };
  }

  docTypeLabel(type: DocType): string {
    switch (type) {
      case 'purchase_order': return 'Purchase Order';
      case 'goods_receipt': return 'Goods Receipt';
      default: return 'Invoice';
    }
  }

  uploadNextInQueue() {
    // Re-entrant guard: prevents drag-dropping/selecting more files
    // while an upload is already in flight from starting a second,
    // concurrent upload — the active item's own completion callback
    // (below) calls this again once it's actually free.
    if (this.isUploading) return;

    // Picks the next item to actually process by DOC_TYPE_UPLOAD_
    // PRIORITY (Invoice -> Purchase Order -> Goods Receipt), never by
    // array/archive-entry order — a ZIP's own internal file ordering
    // must never determine whether a PO is attempted before the
    // Invoice it depends on exists yet. Array.prototype.sort() is
    // stable (ES2019+), so items sharing the same doc type keep their
    // original relative order. This also makes retryFailed() (which
    // resets failed items back to 'pending' in place) automatically
    // reprocess a batch's Invoice before its Purchase Order/Goods
    // Receipt, with no separate retry-ordering logic needed.
    const pendingItems = this.uploadQueue
      .filter(i => i.status === 'pending')
      .sort((a, b) => DOC_TYPE_UPLOAD_PRIORITY[a.docType] - DOC_TYPE_UPLOAD_PRIORITY[b.docType]);

    if (pendingItems.length === 0) {
      this.isUploading = false;
      this.loadDocuments();
      this.saveQueueToStorage();

      const successCount = this.uploadQueue.filter(item => item.status === 'done').length;
      const failCount = this.uploadQueue.filter(item => item.status === 'error').length;
      if (successCount + failCount > 0) {
        this.successMessage = `Batch processing completed: ${successCount} successful, ${failCount} failed.`;
      }
      this.cdr.detectChanges();
      setTimeout(() => {
        this.successMessage = '';
        this.cdr.detectChanges();
      }, 6000);
      return;
    }

    this.isUploading = true;
    const item = pendingItems[0];
    item.status = 'uploading';
    item.progress = 0;
    this.saveQueueToStorage();
    this.cdr.detectChanges();

    if (!item.file || !(item.file instanceof File)) {
      item.status = 'error';
      item.message = 'Please re-select this file';
      this.isUploading = false;
      this.saveQueueToStorage();
      this.cdr.detectChanges();
      this.uploadNextInQueue();
      return;
    }

    if (item.docType !== 'invoice') {
      // purchase_order/goods_receipt reuse the EXISTING "attach
      // supporting document to an invoice" endpoints — they need a
      // target document_id. Prefer the invoice already uploaded
      // earlier in this SAME batch (the ZIP/multi-select this file
      // came from); fall back to whatever document the user has
      // explicitly picked via the page's existing "Attach PO/GR" flow.
      const targetDocumentId = this.batchAnchorInvoice[item.batchId] ?? this.selectedDocumentId;
      if (!targetDocumentId) {
        item.status = 'error';
        // Distinguishes "this batch's own Invoice was attempted but
        // failed" (the exact required message, since retrying it is
        // the actual fix) from "no Invoice was ever part of this batch"
        // (the existing, more general guidance).
        const batchInvoice = this.uploadQueue.find(q => q.batchId === item.batchId && q.docType === 'invoice');
        item.message = batchInvoice?.status === 'error'
          ? 'Invoice upload must complete before supporting documents can be attached.'
          : `No invoice available to attach this ${this.docTypeLabel(item.docType)} to. ` +
            `Upload an Invoice in the same batch first, or select an existing invoice via "Attach PO/GR".`;
        this.isUploading = false;
        this.saveQueueToStorage();
        this.cdr.detectChanges();
        this.uploadNextInQueue();
        return;
      }
      this.uploadQueueItem(item, targetDocumentId);
      return;
    }

    this.uploadQueueItem(item, null);
  }

  // Uploads one confirmed queue item via the appropriate EXISTING
  // endpoint — /documents/upload for an invoice, /documents/upload-po
  // or /documents/upload-gr/<targetDocumentId> for a purchase_order/
  // goods_receipt. Uses HttpClient's progress events (reportProgress +
  // observe: 'events') so the real upload percentage is shown only
  // during the file-transfer stage. HttpEventType.Sent fires the moment
  // the request is DISPATCHED (i.e. at the START of the request, before
  // any bytes are transferred) — it is NOT a "finished sending" signal
  // and must not be used to detect upload completion (confirmed live:
  // Sent arrives before any UploadProgress events at all). The correct
  // signal that the file is fully on the wire and the backend is now
  // running OCR/AI extraction — with no further progress available from
  // the transport layer — is an UploadProgress event whose loaded has
  // reached total; that's when status switches to 'processing' and the
  // template shows an indeterminate indicator instead of a fabricated
  // percentage.
  private uploadQueueItem(item: QueueItem, targetDocumentId: number | null) {
    const formData = new FormData();
    formData.append('document', item.file);
    if (item.docType === 'invoice') {
      formData.append('input_method', 'upload');
    }

    const token = localStorage.getItem('access_token');
    const url = item.docType === 'invoice'
      ? `${this.apiUrl}/documents/upload`
      : item.docType === 'purchase_order'
        ? `${this.apiUrl}/documents/upload-po/${targetDocumentId}`
        : `${this.apiUrl}/documents/upload-gr/${targetDocumentId}`;

    this.http.post<any>(url, formData, {
      headers: new HttpHeaders({ 'Authorization': `Bearer ${token}` }),
      reportProgress: true,
      observe: 'events'
    }).subscribe({
      next: (event: HttpEvent<any>) => {
        if (event.type === HttpEventType.UploadProgress) {
          item.progress = event.total ? Math.round((100 * event.loaded) / event.total) : 0;
          if (event.total && event.loaded >= event.total) {
            item.status = 'processing';
          }
          this.cdr.detectChanges();
        } else if (event.type === HttpEventType.Response) {
          item.status = 'done';
          item.message = item.docType === 'invoice'
            ? 'Uploaded successfully'
            : `${this.docTypeLabel(item.docType)} attached successfully`;
          if (item.docType === 'invoice' && event.body?.document_id) {
            this.batchAnchorInvoice[item.batchId] = event.body.document_id;
          }
          this.isUploading = false;
          this.saveQueueToStorage();
          this.cdr.detectChanges();
          this.uploadNextInQueue();
        }
      },
      error: (err) => {
        item.status = 'error';
        item.message = err.error?.error || 'Upload failed';
        this.isUploading = false;
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
      status: item.status, message: item.message, docType: item.docType
    }));
    localStorage.setItem('uploadQueue', JSON.stringify(simplified));
  }

  loadQueueFromStorage() {
    const saved = localStorage.getItem('uploadQueue');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        this.uploadQueue = parsed.map((item: any): QueueItem => {
          const unresumable = item.status === 'uploading' || item.status === 'pending' || item.status === 'processing';
          return {
            file: { name: item.name, size: item.size } as File,
            docType: (item.docType === 'purchase_order' || item.docType === 'goods_receipt') ? item.docType : 'invoice',
            status: unresumable ? 'error' : item.status,
            message: unresumable ? 'Please re-select this file' : item.message,
            progress: 0,
            batchId: -1,
          };
        });
      } catch (e) { localStorage.removeItem('uploadQueue'); }
    }
  }

  // ── Overall processing summary (drives the processing panel) ──────

  get totalQueued(): number {
    return this.uploadQueue.length;
  }

  get completedCount(): number {
    return this.uploadQueue.filter(item => item.status === 'done').length;
  }

  get processingCount(): number {
    return this.uploadQueue.filter(item => item.status === 'uploading' || item.status === 'processing').length;
  }

  get failedCount(): number {
    return this.uploadQueue.filter(item => item.status === 'error').length;
  }

  // "Processing N of Total" — N is how far through the queue this
  // sequential run has reached: everything finished so far, plus the
  // one item currently active (if any).
  get currentFileNumber(): number {
    const finished = this.completedCount + this.failedCount;
    return this.processingCount > 0 ? finished + 1 : finished;
  }

  get activeQueueItem(): QueueItem | undefined {
    return this.uploadQueue.find(item => item.status === 'uploading' || item.status === 'processing');
  }

  statusLabelFor(status: QueueStatus): string {
    switch (status) {
      case 'pending': return 'Waiting';
      case 'uploading': return 'Uploading';
      case 'processing': return 'OCR and AI Processing';
      case 'done': return 'Completed';
      default: return 'Failed';
    }
  }

  hasRetryableErrors(): boolean {
    return this.uploadQueue.some(item => item.status === 'error' && item.file instanceof File);
  }

  clearDoneItems() {
    this.uploadQueue.filter(item => item.status === 'done' && item.previewUrl)
      .forEach(item => URL.revokeObjectURL(item.previewUrl!));
    this.uploadQueue = this.uploadQueue.filter(item => item.status !== 'done');
    this.saveQueueToStorage();
    this.cdr.detectChanges();
  }

  clearQueue() {
    this.uploadQueue.forEach(item => {
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
    });
    this.uploadQueue = [];
    localStorage.removeItem('uploadQueue');
    this.cdr.detectChanges();
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
      // this.documents now holds GROUPED rows (see computeGroupedRows()
      // above), not raw per-invoice records — remove the deleted
      // invoice from the underlying invoiceRecords and re-derive the
      // grouped view, rather than filtering this.documents directly.
      this.invoiceRecords = this.invoiceRecords.filter(d => d.document_id !== doc.document_id);
      this.computeGroupedRows();
    },
    error: (err) => {
      this.errorMessage = err.error?.error || 'Failed to delete.';
      this.cdr.detectChanges();
    }
  });
}

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }
}