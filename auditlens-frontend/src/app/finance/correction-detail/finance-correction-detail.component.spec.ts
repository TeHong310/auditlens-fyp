import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ActivatedRoute, Router } from '@angular/router';
import { of } from 'rxjs';

import { FinanceCorrectionDetailComponent } from './finance-correction-detail.component';
import { environment } from '../../../environments/environment';

// Correction Detail tests. All HTTP calls are mocked via
// HttpTestingController — no real backend, no AI calls. Every endpoint
// called here (GET /documents/<id>, GET /reviews/send-back-cycles/<id>,
// GET /ocr-review/invoice/<id>/related-docs, PUT /documents/<id>/
// update-fields, POST /documents/upload-po|upload-gr/<id>, POST
// /reviews/resubmit/<id>) is a pre-existing endpoint reused as-is.
describe('FinanceCorrectionDetailComponent', () => {
  let component: FinanceCorrectionDetailComponent;
  let httpMock: HttpTestingController;
  let navigateCalls: any[][];

  beforeEach(async () => {
    navigateCalls = [];
    await TestBed.configureTestingModule({
      imports: [FinanceCorrectionDetailComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { queryParams: of({}) } },
        { provide: Router, useValue: { navigate: (...args: any[]) => { navigateCalls.push(args); return Promise.resolve(true); } } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(FinanceCorrectionDetailComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    component.documentId = 42; // ngOnInit never runs (detectChanges not called) — set directly
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('Finance can open the correction detail — loads invoice, cycle, and related-docs on loadAll()', () => {
    component.loadAll();

    const docReq = httpMock.expectOne(`${environment.apiUrl}/documents/42`);
    expect(docReq.request.method).toBe('GET');
    docReq.flush({
      document: {
        document_id: 42, invoice_number: 'INV-PWE-2026-S147', vendor_name: 'Primewave Electronics Sdn. Bhd.',
        total_amount: 1250.00, currency: 'RM', invoice_date: '2026-07-15', status: 'returned', file_name: 'invoice.pdf',
      }
    });

    const cycleReq = httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/42`);
    cycleReq.flush({
      cycles: [{
        cycle_number: 1, return_reason_category: 'missing_document',
        auditor_instruction: 'Please upload the Purchase Order and Goods Receipt.',
        required_actions: ['upload_missing_document'], priority: 'medium', cycle_status: 'action_required',
      }]
    });

    const relatedReq = httpMock.expectOne(`${environment.apiUrl}/ocr-review/invoice/42/related-docs`);
    relatedReq.flush({ po: { uploaded: false }, gr: { uploaded: false } });

    expect(component.document.invoice_number).toBe('INV-PWE-2026-S147');
    expect(component.document.vendor_name).toBe('Primewave Electronics Sdn. Bhd.');
    expect(component.latestCycle.auditor_instruction).toBe('Please upload the Purchase Order and Goods Receipt.');
    expect(component.reasonCategoryLabel(component.latestCycle.return_reason_category)).toBe('Missing document');
    expect(component.relatedDocs.po.uploaded).toBe(false);
    expect(component.relatedDocs.gr.uploaded).toBe(false);
    expect(component.isLoading).toBe(false);
  });

  it('a document with no structured cycle shows the "no structured request" fallback (latestCycle is null)', () => {
    component.loadAll();

    httpMock.expectOne(`${environment.apiUrl}/documents/42`).flush({
      document: { document_id: 42, invoice_number: 'INV-1', status: 'returned' }
    });
    httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/42`).flush({ cycles: [] });
    httpMock.expectOne(`${environment.apiUrl}/ocr-review/invoice/42/related-docs`).flush({ po: { uploaded: false }, gr: { uploaded: false } });

    expect(component.latestCycle).toBeNull();
  });

  it('saveChanges PUTs to /documents/<id>/update-fields with the edited fields', () => {
    component.editFields = { invoice_number: 'INV-EDITED', vendor_name: 'Vendor Edited', invoice_date: '2026-07-16', total_amount: '999.50', tax_amount: '' };
    component.saveChanges();

    const req = httpMock.expectOne(`${environment.apiUrl}/documents/42/update-fields`);
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({
      invoice_number: 'INV-EDITED', vendor_name: 'Vendor Edited', invoice_date: '2026-07-16',
      total_amount: '999.50', tax_amount: null,
    });
    req.flush({ message: 'Fields updated successfully' });

    expect(component.isSaving).toBe(false);
    expect(component.successMessage).toBe('Changes saved successfully!');
  });

  it('uploadPO POSTs to /documents/upload-po/<id> and refreshes related-docs on success', () => {
    const file = new File(['x'], 'po.pdf', { type: 'application/pdf' });
    component.uploadPO(file);

    const uploadReq = httpMock.expectOne(`${environment.apiUrl}/documents/upload-po/42`);
    expect(uploadReq.request.method).toBe('POST');
    uploadReq.flush({ extracted_fields: { po_number: 'PO-2026-0087' } });

    const relatedReq = httpMock.expectOne(`${environment.apiUrl}/ocr-review/invoice/42/related-docs`);
    relatedReq.flush({ po: { uploaded: true, po_no: 'PO-2026-0087' }, gr: { uploaded: false } });

    expect(component.isUploadingPO).toBe(false);
    expect(component.poMessage).toContain('PO-2026-0087');
    expect(component.relatedDocs.po.uploaded).toBe(true);
  });

  it('resubmit blocks without a Finance response and never calls the API', () => {
    component.financeResponse = '   ';
    component.resubmit();

    expect(component.errorMessage).toBe('Please add a Finance response before resubmitting.');
    httpMock.expectNone(`${environment.apiUrl}/reviews/resubmit/42`);
  });

  it('resubmit works — POSTs to /reviews/resubmit/<id> with the response and navigates back to the list', () => {
    component.financeResponse = 'Purchase Order and Goods Receipt have been uploaded as requested.';
    component.resubmit();

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/resubmit/42`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ response: 'Purchase Order and Goods Receipt have been uploaded as requested.' });
    req.flush({ message: 'Document resubmitted for review', status: 'resubmitted' });

    expect(component.isSubmitting).toBe(false);
    expect(component.successMessage).toBe('Invoice resubmitted to Auditor successfully!');
  });

  it('resubmit surfaces a server-side error and does not navigate away', () => {
    component.financeResponse = 'Some response';
    component.resubmit();

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/resubmit/42`);
    req.flush({ error: 'Document is not returned. Current status: under_review' }, { status: 400, statusText: 'Bad Request' });

    expect(component.isSubmitting).toBe(false);
    expect(component.errorMessage).toBe('Document is not returned. Current status: under_review');
    expect(navigateCalls.length).toBe(0);
  });

  it('goBack navigates to the Correction Center list', () => {
    component.goBack();
    expect(navigateCalls[0][0]).toEqual(['/finance/corrections']);
  });
});
