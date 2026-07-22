import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';

import { FinanceOcrReviewComponent } from './finance-ocr-review.component';
import { environment } from '../../../environments/environment';

// OCR extraction review + field editing only — the returned-invoice
// correction workflow (Auditor Request card, Finance Response field,
// Resubmit to Auditor) has moved to Finance Correction Center
// (finance/correction-detail.component.ts) and no longer exists here.
// All HTTP calls are mocked — no real backend, no AI calls.
describe('FinanceOcrReviewComponent — OCR review only (correction workflow removed)', () => {
  let component: FinanceOcrReviewComponent;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FinanceOcrReviewComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
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

  it('loadDocuments only includes ocr_done documents — returned invoices never appear here', () => {
    component.loadDocuments();

    const req = httpMock.expectOne(`${environment.apiUrl}/documents/`);
    expect(req.request.method).toBe('GET');
    req.flush({
      documents: [
        { document_id: 1, status: 'ocr_done', invoice_number: 'INV-1' },
        { document_id: 2, status: 'returned', invoice_number: 'INV-2' },
        { document_id: 3, status: 'under_review', invoice_number: 'INV-3' },
        { document_id: 4, status: 'approved', invoice_number: 'INV-4' },
      ]
    });

    // loadDocuments() also kicks off related-docs/PO/GR requests for
    // the ocr_done set — flush the ones triggered so httpMock.verify()
    // in afterEach doesn't fail on outstanding requests.
    httpMock.match(() => true).forEach(req => req.flush({}));

    expect(component.documents.length).toBe(1);
    expect(component.documents[0].document_id).toBe(1);
  });

  it('selecting a document never fetches any send-back cycle data', () => {
    const doc = { document_id: 5, status: 'ocr_done', invoice_number: 'INV-5', vendor_name: 'Acme' };
    component.documents = [doc];

    component.selectDocument(doc);

    expect(component.selectedDoc).toBe(doc);
    httpMock.expectNone(r => r.url.includes('/reviews/send-back-cycles/'));
  });

  it('canSubmit only checks that required fields are filled — no Finance-response requirement remains', () => {
    component.editFields = { invoice_number: '', vendor_name: 'Acme', invoice_date: '2026-07-01', total_amount: '100' };
    expect(component.canSubmit()).toBe(false);

    component.editFields = { invoice_number: 'INV-1', vendor_name: 'Acme', invoice_date: '2026-07-01', total_amount: '100' };
    expect(component.canSubmit()).toBe(true);
  });

  it('submitToAuditor always POSTs an empty body to /reviews/submit/<id>', () => {
    component.editFields = { invoice_number: 'INV-1', vendor_name: 'Acme', invoice_date: '2026-07-01', total_amount: '100' };
    component.selectedDoc = { document_id: 8, status: 'ocr_done' };
    component.documents = [component.selectedDoc];

    component.submitToAuditor();

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/submit/8`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});
    req.flush({ message: 'ok' });

    expect(component.successMessage).toBe('Document submitted to Auditor successfully!');
    expect(component.selectedDoc).toBeNull();
  });

  it('blocks submission with a clear message when required fields are missing', () => {
    component.editFields = { invoice_number: '', vendor_name: '', invoice_date: '', total_amount: '' };
    component.selectedDoc = { document_id: 9, status: 'ocr_done' };

    component.submitToAuditor();

    expect(component.errorMessage).toBe('Please fill in all required fields before submitting.');
    httpMock.expectNone(`${environment.apiUrl}/reviews/submit/9`);
  });

  it('getErrorType never reports "Returned" — that state cannot reach this page anymore', () => {
    const doc = { invoice_number: 'INV-1', vendor_name: 'Acme', total_amount: 100, ocr_confidence: 90, status: 'ocr_done' };
    expect(component.getErrorType(doc)).toBe('Ready');
    expect(component.getErrorClass(doc)).toBe('badge-ready');
  });

  it('(FinanceOcrReviewComponent as any) no longer exposes the removed correction-workflow API', () => {
    const anyComponent = component as any;
    expect(anyComponent.returnedDocuments).toBeUndefined();
    expect(anyComponent.loadSelectedDocCycle).toBeUndefined();
    expect(anyComponent.reasonCategoryLabel).toBeUndefined();
    expect(anyComponent.requiredActionLabel).toBeUndefined();
    expect(anyComponent.goToUpload).toBeUndefined();
    expect(anyComponent.financeResponse).toBeUndefined();
    expect(anyComponent.selectedDocCycle).toBeUndefined();
  });
});
