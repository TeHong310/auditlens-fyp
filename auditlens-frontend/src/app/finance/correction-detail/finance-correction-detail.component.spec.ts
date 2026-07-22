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

    expect(component.errorMessage).toBe('Please add a Finance response before submitting.');
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
    expect(component.successMessage).toBe('Correction submitted for auditor review successfully!');
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

// AI Correction Assistant tests — every action is triggered ONLY by an
// explicit method call (mirroring a button click), never by ngOnInit.
// All HTTP calls are mocked via HttpTestingController — no real
// backend, no real Claude/Gemini calls.
describe('FinanceCorrectionDetailComponent — AI Correction Assistant', () => {
  let component: FinanceCorrectionDetailComponent;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FinanceCorrectionDetailComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { queryParams: of({}) } },
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(FinanceCorrectionDetailComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    component.documentId = 42;
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('never calls the AI Assistant automatically — no request fires without an explicit action', () => {
    httpMock.expectNone(r => r.url.includes('/ai-assistant/'));
  });

  it('explainIssue POSTs to /ai-assistant/<id>/finance/explain-issue and stores the structured audit-status summary', () => {
    component.explainIssue();
    expect(component.aiActionLoading['explain_issue']).toBe(true);

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/42/finance/explain-issue`);
    expect(req.request.method).toBe('POST');
    req.flush({
      audit_status: 'REVIEW REQUIRED',
      reason: 'This invoice cannot complete validation because the Purchase Order and Goods Receipt documents are missing.',
      recommended_action: 'Upload the missing supporting documents and resubmit for auditor review.',
      provider: 'claude', cached: false,
    });

    expect(component.aiActionLoading['explain_issue']).toBe(false);
    expect(component.aiCaseSummary).toEqual({
      audit_status: 'REVIEW REQUIRED',
      reason: 'This invoice cannot complete validation because the Purchase Order and Goods Receipt documents are missing.',
      recommended_action: 'Upload the missing supporting documents and resubmit for auditor review.',
    });
    expect(component.auditStatusClass('REVIEW REQUIRED')).toBe('badge-returned');
    expect(component.auditStatusClass('PASS')).toBe('badge-ready');
  });

  it('generateResponse POSTs to /ai-assistant/<id>/finance/generate-response and fills the EXISTING Finance Response field, without resubmitting', () => {
    component.financeResponse = '';
    component.generateResponse();

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/42/finance/generate-response`);
    expect(req.request.method).toBe('POST');
    req.flush({
      response: 'Purchase Order and Goods Receipt documents have been obtained and uploaded. The invoice has been reviewed and is ready for auditor revalidation.',
      provider: 'claude', cached: false,
    });

    expect(component.financeResponse).toBe(
      'Purchase Order and Goods Receipt documents have been obtained and uploaded. The invoice has been reviewed and is ready for auditor revalidation.'
    );
    // Never auto-submitted — resubmission only happens via the
    // existing resubmit() flow when Finance clicks the button.
    httpMock.expectNone(`${environment.apiUrl}/reviews/resubmit/42`);
  });

  it('recommendedSteps POSTs to /ai-assistant/<id>/finance/recommended-steps and stores the step list', () => {
    component.recommendedSteps();

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/42/finance/recommended-steps`);
    expect(req.request.method).toBe('POST');
    req.flush({
      steps: ['Upload missing documents', 'Verify invoice information', 'Add explanation', 'Resubmit to auditor'],
      provider: 'claude', cached: false,
    });

    expect(component.aiSteps).toEqual([
      'Upload missing documents', 'Verify invoice information', 'Add explanation', 'Resubmit to auditor'
    ]);
  });

  it('askAiQuestion POSTs the question to /ai-assistant/<id>/finance/ask and appends the Q&A to the conversation log', () => {
    component.aiQuestion = 'What documents are missing?';
    component.askAiQuestion();

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/42/finance/ask`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ question: 'What documents are missing?' });
    req.flush({ answer: 'The Purchase Order and Goods Receipt are missing.', provider: 'claude', cached: false });

    expect(component.aiConversation).toEqual([
      { question: 'What documents are missing?', answer: 'The Purchase Order and Goods Receipt are missing.' }
    ]);
    expect(component.aiQuestion).toBe('');
  });

  it('askAiQuestion does nothing for a blank question', () => {
    component.aiQuestion = '   ';
    component.askAiQuestion();
    httpMock.expectNone(r => r.url.includes('/ai-assistant/'));
  });

  it('surfaces a 502 AI-unavailable error without crashing, and clears the loading state', () => {
    component.explainIssue();
    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/42/finance/explain-issue`);
    req.flush({ error: 'AI Assistant is unavailable right now — see server logs' }, { status: 502, statusText: 'Bad Gateway' });

    expect(component.aiActionLoading['explain_issue']).toBe(false);
    expect(component.aiError).toBe('AI Assistant is unavailable right now — see server logs');
  });
});
