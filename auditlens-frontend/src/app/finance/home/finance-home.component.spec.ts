import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { Router } from '@angular/router';

import { FinanceHomeComponent } from './finance-home.component';
import { environment } from '../../../environments/environment';

// Action-oriented AP workflow dashboard tests. All HTTP calls are
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

  it('a returned document is clickable, shows "Fix Issue", and navigates to Correction Center detail', () => {
    const doc = { document_id: 7, status: 'returned', invoice_number: 'INV-PWE-2026-S147' };

    expect(component.actionLabel(doc)).toBe('Fix Issue');
    expect(component.isActionClickable(doc)).toBe(true);

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

  // ── 2. Under review invoice -> "Waiting Auditor" -> no action ──

  it('an under_review document is NOT clickable, shows "Waiting Auditor", and onAction never navigates', () => {
    const doc = { document_id: 3, status: 'under_review' };

    expect(component.actionLabel(doc)).toBe('Waiting Auditor');
    expect(component.isActionClickable(doc)).toBe(false);

    component.onAction(doc);
    expect(navigateCalls.length).toBe(0);
  });

  it('a resubmitted document is NOT clickable and also shows "Waiting Auditor"', () => {
    const doc = { document_id: 13, status: 'resubmitted' };
    expect(component.actionLabel(doc)).toBe('Waiting Auditor');
    expect(component.isActionClickable(doc)).toBe(false);
  });

  it('an under_review document shows "Pending Auditor Review" as its Audit Status and "-" for Required Action', () => {
    const doc = { document_id: 3, status: 'under_review' };
    expect(component.auditStatusLabel(doc)).toBe('Pending Auditor Review');
    expect(component.requiredActionLabel(doc)).toBe('-');
  });

  // ── 3. Approved invoice -> "No Action" -> no action ──

  it('an approved document is NOT clickable, shows "No Action", and onAction never navigates', () => {
    const doc = { document_id: 9, status: 'approved' };

    expect(component.actionLabel(doc)).toBe('No Action');
    expect(component.isActionClickable(doc)).toBe(false);

    component.onAction(doc);
    expect(navigateCalls.length).toBe(0);
  });

  it('an approved document shows "Approved" as its Audit Status and "-" for Required Action', () => {
    const doc = { document_id: 9, status: 'approved' };
    expect(component.auditStatusLabel(doc)).toBe('Approved');
    expect(component.requiredActionLabel(doc)).toBe('-');
  });

  // ── 4. Processing / not-yet-submitted -> "Review" -> OCR Review page ──

  it('an ocr_processing document is clickable, shows "Review", and navigates to the OCR Review page', () => {
    const doc = { document_id: 11, status: 'ocr_processing' };

    expect(component.actionLabel(doc)).toBe('Review');
    expect(component.isActionClickable(doc)).toBe(true);

    component.onAction(doc);
    expect(navigateCalls[0][0]).toEqual(['/finance/ocr-review']);
  });

  it('an ocr_done document is clickable, shows "Review", and navigates to the OCR Review page', () => {
    const doc = { document_id: 12, status: 'ocr_done' };
    expect(component.actionLabel(doc)).toBe('Review');
    expect(component.isActionClickable(doc)).toBe(true);
    component.onAction(doc);
    expect(navigateCalls[0][0]).toEqual(['/finance/ocr-review']);
  });

  // ── Quick navigation buttons ──

  it('goToCorrections navigates to the Correction Center', () => {
    component.goToCorrections();
    expect(navigateCalls[0][0]).toEqual(['/finance/corrections']);
  });

  it('goToUpload navigates to Upload Document', () => {
    component.goToUpload();
    expect(navigateCalls[0][0]).toEqual(['/finance/upload']);
  });

  it('goToOcrReview navigates to OCR Review', () => {
    component.goToOcrReview();
    expect(navigateCalls[0][0]).toEqual(['/finance/ocr-review']);
  });

  // ── loadCyclesForActionStats: fetches cycles for EVERY returned
  // document passed in (not just a slice), and derives missingDocsCount
  // from required_actions containing 'upload_missing_document' ──

  it('loadCyclesForActionStats only calls send-back-cycles for the returned documents passed in', () => {
    const returnedThisMonth = [{ document_id: 2, status: 'returned' }];
    (component as any).loadCyclesForActionStats(returnedThisMonth);

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/2`);
    req.flush({ cycles: [{ cycle_number: 1, required_actions: ['upload_missing_document'] }] });

    expect(component.latestCycleByDocId[2].required_actions).toEqual(['upload_missing_document']);
    expect(component.missingDocsCount).toBe(1);
  });

  it('loadCyclesForActionStats counts only cases whose required_actions include upload_missing_document', () => {
    const returnedThisMonth = [
      { document_id: 2, status: 'returned' },
      { document_id: 4, status: 'returned' },
    ];
    (component as any).loadCyclesForActionStats(returnedThisMonth);

    httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/2`)
      .flush({ cycles: [{ cycle_number: 1, required_actions: ['upload_missing_document'] }] });
    httpMock.expectOne(`${environment.apiUrl}/reviews/send-back-cycles/4`)
      .flush({ cycles: [{ cycle_number: 1, required_actions: ['verify_amount_or_quantity'] }] });

    expect(component.missingDocsCount).toBe(1);
  });

  it('loadCyclesForActionStats sets missingDocsCount to 0 and makes no request when nothing is returned', () => {
    (component as any).loadCyclesForActionStats([]);
    httpMock.expectNone(r => r.url.includes('/reviews/send-back-cycles/'));
    expect(component.missingDocsCount).toBe(0);
  });
});
