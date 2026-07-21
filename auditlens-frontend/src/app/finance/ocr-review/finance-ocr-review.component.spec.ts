import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { Router } from '@angular/router';

import { FinanceOcrReviewComponent } from './finance-ocr-review.component';
import { environment } from '../../../environments/environment';

// Returned-for-Correction + Finance Response + Resubmission tests
// (Features 2, 3). All HTTP calls are mocked — no real backend, no AI
// calls.

describe('FinanceOcrReviewComponent — Returned for Correction workflow', () => {
  let component: FinanceOcrReviewComponent;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FinanceOcrReviewComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(FinanceOcrReviewComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    // ngOnInit is never triggered (detectChanges() not called), so no
    // automatic HTTP calls happen — each test drives state directly.
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('returnedDocuments filters to only status === "returned"', () => {
    component.documents = [
      { document_id: 1, status: 'ocr_done' },
      { document_id: 2, status: 'returned' },
      { document_id: 3, status: 'returned' },
      { document_id: 4, status: 'under_review' },
    ];
    expect(component.returnedDocuments.length).toBe(2);
    expect(component.returnedDocuments.map(d => d.document_id)).toEqual([2, 3]);
  });

  it('selecting a returned document loads its send-back cycle and shows the latest one', () => {
    const doc = { document_id: 5, status: 'returned', invoice_number: 'INV-1', vendor_name: 'Acme' };
    component.documents = [doc];

    component.selectDocument(doc);

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/5`);
    expect(req.request.method).toBe('GET');
    req.flush({
      document_id: 5,
      cycles: [
        { cycle_number: 1, cycle_status: 'resolved', auditor_instruction: 'First instruction' },
        { cycle_number: 2, cycle_status: 'action_required', auditor_instruction: 'Second instruction',
          return_reason_category: 'possible_duplicate_invoice', priority: 'high',
          required_actions: ['provide_written_explanation'] },
      ],
    });

    expect(component.selectedDocCycle).toBeTruthy();
    expect(component.selectedDocCycle.cycle_number).toBe(2);
    expect(component.selectedDocCycle.auditor_instruction).toBe('Second instruction');
  });

  it('selecting a non-returned document does not fetch any cycle', () => {
    const doc = { document_id: 6, status: 'ocr_done' };
    component.documents = [doc];
    component.selectDocument(doc);
    expect(component.selectedDocCycle).toBeNull();
    httpMock.expectNone(`${environment.apiUrl}/reviews/send-back-cycles/6`);
  });

  it('canSubmit requires a Finance response only when the document was returned', () => {
    component.editFields = { invoice_number: 'INV-1', vendor_name: 'Acme', invoice_date: '2026-07-01', total_amount: '100' };

    component.selectedDoc = { document_id: 1, status: 'ocr_done' };
    expect(component.canSubmit()).toBe(true);

    component.selectedDoc = { document_id: 2, status: 'returned' };
    component.financeResponse = '';
    expect(component.canSubmit()).toBe(false);

    component.financeResponse = 'The duplicate invoice was withdrawn.';
    expect(component.canSubmit()).toBe(true);
  });

  it('submitToAuditor sends the Finance response to /reviews/resubmit/<id> for a returned document', () => {
    component.editFields = { invoice_number: 'INV-1', vendor_name: 'Acme', invoice_date: '2026-07-01', total_amount: '100' };
    component.selectedDoc = { document_id: 7, status: 'returned' };
    component.financeResponse = 'This invoice was accidentally uploaded twice.';
    component.documents = [component.selectedDoc];

    component.submitToAuditor();

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/resubmit/7`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ response: 'This invoice was accidentally uploaded twice.' });
    req.flush({ message: 'ok' });

    expect(component.selectedDoc).toBeNull();
    expect(component.financeResponse).toBe('');
  });

  it('submitToAuditor sends an empty body to /reviews/submit/<id> for a first-time (non-returned) submission', () => {
    component.editFields = { invoice_number: 'INV-1', vendor_name: 'Acme', invoice_date: '2026-07-01', total_amount: '100' };
    component.selectedDoc = { document_id: 8, status: 'ocr_done' };
    component.documents = [component.selectedDoc];

    component.submitToAuditor();

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/submit/8`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});
    req.flush({ message: 'ok' });
  });

  it('blocks resubmission and shows a clear message when no Finance response was written', () => {
    component.editFields = { invoice_number: 'INV-1', vendor_name: 'Acme', invoice_date: '2026-07-01', total_amount: '100' };
    component.selectedDoc = { document_id: 9, status: 'returned' };
    component.financeResponse = '';

    component.submitToAuditor();

    expect(component.errorMessage).toContain('Finance response');
    httpMock.expectNone(`${environment.apiUrl}/reviews/resubmit/9`);
  });

  it('maps machine-readable reason/action keys to human-readable labels', () => {
    expect(component.reasonCategoryLabel('possible_duplicate_invoice')).toBe('Possible duplicate invoice');
    expect(component.requiredActionLabel('confirm_duplicate_submission')).toBe('Confirm duplicate submission');
    expect(component.reasonCategoryLabel('unknown_key')).toBe('unknown_key');
  });
});
