import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { Router } from '@angular/router';

import { FinanceCorrectionsComponent } from './finance-corrections.component';
import { environment } from '../../../environments/environment';

// Correction Center list tests. All HTTP calls are mocked via
// HttpTestingController — no real backend, no AI calls. The whole list
// is built by combining two EXISTING endpoints (GET /documents/ and
// GET /reviews/send-back-cycles/<id>) — no new backend code exists to
// mock beyond that.
describe('FinanceCorrectionsComponent', () => {
  let component: FinanceCorrectionsComponent;
  let httpMock: HttpTestingController;
  let navigateCalls: any[][];

  beforeEach(async () => {
    navigateCalls = [];
    await TestBed.configureTestingModule({
      imports: [FinanceCorrectionsComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Router, useValue: { navigate: (...args: any[]) => { navigateCalls.push(args); return Promise.resolve(true); } } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(FinanceCorrectionsComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    // ngOnInit is never triggered (detectChanges() not called) — each
    // test drives loadCorrections() explicitly.
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('a returned invoice appears in the list', () => {
    component.loadCorrections();

    const docsReq = httpMock.expectOne(`${environment.apiUrl}/documents/`);
    docsReq.flush({
      documents: [
        { document_id: 1, invoice_number: 'INV-PWE-2026-S147', vendor_name: 'Primewave Electronics Sdn. Bhd.', status: 'returned', updated_at: '2026-07-20T10:00:00' },
      ]
    });

    const cycleReq = httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/1`);
    cycleReq.flush({
      cycles: [{
        cycle_number: 1, return_reason_category: 'missing_document',
        required_actions: ['upload_missing_document'], priority: 'medium',
        sent_back_at: '2026-07-20T09:00:00', cycle_status: 'action_required',
      }]
    });

    expect(component.corrections.length).toBe(1);
    expect(component.corrections[0].invoice_number).toBe('INV-PWE-2026-S147');
    expect(component.returnReasonLabel(component.corrections[0])).toBe('Missing document');
    expect(component.requiredActionsSummary(component.corrections[0])).toBe('Upload missing document');
    expect(component.priorityLabel(component.corrections[0])).toBe('Medium');
    expect(component.currentStatusLabel(component.corrections[0])).toBe('Awaiting Finance Correction');
  });

  it('a non-returned invoice does NOT appear in the list', () => {
    component.loadCorrections();

    const docsReq = httpMock.expectOne(`${environment.apiUrl}/documents/`);
    docsReq.flush({
      documents: [
        { document_id: 1, invoice_number: 'INV-A', vendor_name: 'Vendor A', status: 'under_review' },
        { document_id: 2, invoice_number: 'INV-B', vendor_name: 'Vendor B', status: 'ocr_done' },
        { document_id: 3, invoice_number: 'INV-C', vendor_name: 'Vendor C', status: 'approved' },
      ]
    });

    // No send-back-cycles call for any of them — none are 'returned'.
    httpMock.expectNone(r => r.url.includes('/reviews/send-back-cycles/'));

    expect(component.corrections.length).toBe(0);
  });

  it('a mixed response only shows the returned invoice, not the others', () => {
    component.loadCorrections();

    const docsReq = httpMock.expectOne(`${environment.apiUrl}/documents/`);
    docsReq.flush({
      documents: [
        { document_id: 1, invoice_number: 'INV-RETURNED', vendor_name: 'Vendor A', status: 'returned', updated_at: '2026-07-20T10:00:00' },
        { document_id: 2, invoice_number: 'INV-APPROVED', vendor_name: 'Vendor B', status: 'approved' },
      ]
    });

    // Only document_id=1 (the returned one) gets a cycle lookup.
    const cycleReq = httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/1`);
    cycleReq.flush({ cycles: [] });

    expect(component.corrections.length).toBe(1);
    expect(component.corrections[0].invoice_number).toBe('INV-RETURNED');
  });

  it('a returned invoice with no structured cycle (legacy return) still appears, with fallback labels', () => {
    component.loadCorrections();

    const docsReq = httpMock.expectOne(`${environment.apiUrl}/documents/`);
    docsReq.flush({
      documents: [
        { document_id: 5, invoice_number: 'INV-LEGACY', vendor_name: 'Vendor X', status: 'returned', updated_at: '2026-07-18T08:00:00' },
      ]
    });

    const cycleReq = httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/5`);
    cycleReq.flush({ cycles: [] });

    expect(component.corrections.length).toBe(1);
    expect(component.returnReasonLabel(component.corrections[0])).toBe('Sent back to Finance');
    expect(component.requiredActionsSummary(component.corrections[0])).toBe('-');
    expect(component.priorityLabel(component.corrections[0])).toBe('Normal');
  });

  it('an empty documents list shows zero corrections and never calls send-back-cycles', () => {
    component.loadCorrections();
    const docsReq = httpMock.expectOne(`${environment.apiUrl}/documents/`);
    docsReq.flush({ documents: [] });

    httpMock.expectNone(r => r.url.includes('/reviews/send-back-cycles/'));
    expect(component.corrections).toEqual([]);
    expect(component.isLoading).toBe(false);
  });

  it('Finance can open the correction detail page for a returned invoice', () => {
    const doc = { document_id: 7, invoice_number: 'INV-7', status: 'returned' };
    component.openCorrection(doc);

    expect(navigateCalls[0][0]).toEqual(['/finance/corrections/detail']);
    expect(navigateCalls[0][1]).toEqual({ queryParams: { document_id: 7 } });
  });

  it('search filters by invoice number or vendor name', () => {
    component.corrections = [
      { document_id: 1, invoice_number: 'INV-AAA', vendor_name: 'Coilcraft Singapore' },
      { document_id: 2, invoice_number: 'INV-BBB', vendor_name: 'Primewave Electronics' },
    ];
    component.searchText = 'primewave';
    expect(component.filteredCorrections.length).toBe(1);
    expect(component.filteredCorrections[0].invoice_number).toBe('INV-BBB');

    component.searchText = 'INV-AAA';
    expect(component.filteredCorrections.length).toBe(1);
    expect(component.filteredCorrections[0].vendor_name).toBe('Coilcraft Singapore');
  });
});
