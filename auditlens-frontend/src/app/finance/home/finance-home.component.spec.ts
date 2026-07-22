import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { Router } from '@angular/router';

import { FinanceHomeComponent } from './finance-home.component';
import { environment } from '../../../environments/environment';

// Recent Uploads dynamic action button tests. All HTTP calls are
// mocked via HttpTestingController — no real backend. Chart rendering
// is untouched by this task and isn't exercised here (ngOnInit is
// never triggered; each test drives the methods under test directly).
describe('FinanceHomeComponent — Recent Uploads action routing', () => {
  let component: FinanceHomeComponent;
  let httpMock: HttpTestingController;
  let navigateCalls: any[][];

  beforeEach(async () => {
    navigateCalls = [];
    await TestBed.configureTestingModule({
      imports: [FinanceHomeComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Router, useValue: { navigate: (...args: any[]) => { navigateCalls.push(args); return Promise.resolve(true); } } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(FinanceHomeComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  // ── 1. Returned invoice -> "Fix Issue" -> Correction Center ──

  it('a returned document shows "Fix Issue" and navigates to Correction Center detail', () => {
    const doc = { document_id: 7, status: 'returned', invoice_number: 'INV-PWE-2026-S147' };

    expect(component.actionLabel(doc)).toBe('Fix Issue');

    component.onAction(doc);
    expect(navigateCalls[0][0]).toEqual(['/finance/corrections/detail']);
    expect(navigateCalls[0][1]).toEqual({ queryParams: { document_id: 7 } });
  });

  it('a returned document shows "Correction Required" as its Audit Status', () => {
    const doc = { document_id: 7, status: 'returned' };
    expect(component.auditStatusLabel(doc)).toBe('Correction Required');
    expect(component.auditStatusClass(doc)).toBe('badge-returned');
  });

  it('Required Action for a returned document uses the loaded send_back_cycles data when available', () => {
    const doc = { document_id: 7, status: 'returned' };
    component.latestCycleByDocId[7] = { required_actions: ['upload_missing_document'] };
    expect(component.requiredActionLabel(doc)).toBe('Upload missing document');
  });

  it('Required Action falls back to a generic message when no cycle data is available yet (legacy return)', () => {
    const doc = { document_id: 8, status: 'returned' };
    // No entry in latestCycleByDocId — e.g. still loading, or a legacy
    // return with no structured cycle at all.
    expect(component.requiredActionLabel(doc)).toBe('Awaiting Finance correction');
  });

  // ── 2. Under review invoice -> "View" -> existing OCR Review page ──

  it('an under_review document shows "View" and navigates to the OCR Review page', () => {
    const doc = { document_id: 3, status: 'under_review' };

    expect(component.actionLabel(doc)).toBe('View');

    component.onAction(doc);
    expect(navigateCalls[0][0]).toEqual(['/finance/ocr-review']);
  });

  it('an under_review document shows "Pending Auditor Review" as its Audit Status and "-" for Required Action', () => {
    const doc = { document_id: 3, status: 'under_review' };
    expect(component.auditStatusLabel(doc)).toBe('Pending Auditor Review');
    expect(component.requiredActionLabel(doc)).toBe('-');
  });

  // ── 3. Approved invoice -> "View" -> opens the document file directly ──

  it('an approved document shows "View" and does NOT navigate to any page (opens the file directly instead)', () => {
    const doc = { document_id: 9, status: 'approved' };
    // viewDocument() uses a raw fetch() to open a blob in a new tab —
    // not a router navigation. The meaningful assertion here is that
    // onAction() for 'approved' never calls router.navigate at all.
    const originalFetch = (globalThis as any).fetch;
    (globalThis as any).fetch = () => new Promise(() => { /* never resolves — avoids touching window.open/alert in jsdom */ });

    expect(component.actionLabel(doc)).toBe('View');
    component.onAction(doc);

    expect(navigateCalls.length).toBe(0);
    (globalThis as any).fetch = originalFetch;
  });

  it('an approved document shows "Approved" as its Audit Status and "-" for Required Action', () => {
    const doc = { document_id: 9, status: 'approved' };
    expect(component.auditStatusLabel(doc)).toBe('Approved');
    expect(component.requiredActionLabel(doc)).toBe('-');
  });

  // ── 4. Processing / not-yet-submitted -> "Review" -> OCR Review page ──

  it('an ocr_processing document shows "Review" and navigates to the OCR Review page', () => {
    const doc = { document_id: 11, status: 'ocr_processing' };

    expect(component.actionLabel(doc)).toBe('Review');

    component.onAction(doc);
    expect(navigateCalls[0][0]).toEqual(['/finance/ocr-review']);
  });

  it('an ocr_done document shows "Review" and navigates to the OCR Review page', () => {
    const doc = { document_id: 12, status: 'ocr_done' };
    expect(component.actionLabel(doc)).toBe('Review');
    component.onAction(doc);
    expect(navigateCalls[0][0]).toEqual(['/finance/ocr-review']);
  });

  // ── loadCyclesForReturnedRows: only fetches for returned rows, at most 5 ──

  it('loadCyclesForReturnedRows only calls send-back-cycles for returned documents in the current list', () => {
    component.documents = [
      { document_id: 1, status: 'approved' },
      { document_id: 2, status: 'returned' },
      { document_id: 3, status: 'under_review' },
    ];
    (component as any).loadCyclesForReturnedRows();

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/2`);
    req.flush({ cycles: [{ cycle_number: 1, required_actions: ['upload_missing_document'] }] });

    expect(component.latestCycleByDocId[2].required_actions).toEqual(['upload_missing_document']);
    httpMock.expectNone(`${environment.apiUrl}/reviews/send-back-cycles/1`);
    httpMock.expectNone(`${environment.apiUrl}/reviews/send-back-cycles/3`);
  });

  it('loadCyclesForReturnedRows makes no request at all when nothing is returned', () => {
    component.documents = [
      { document_id: 1, status: 'approved' },
      { document_id: 3, status: 'under_review' },
    ];
    (component as any).loadCyclesForReturnedRows();
    httpMock.expectNone(r => r.url.includes('/reviews/send-back-cycles/'));
  });
});
