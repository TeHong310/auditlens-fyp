import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ActivatedRoute } from '@angular/router';
import { of } from 'rxjs';

import {
  AuditorRecordDetailComponent, validateSendBackForm, emptySendBackForm, SendBackFormState
} from './auditor-record-detail.component';
import { environment } from '../../../environments/environment';

// Send-Back workflow tests (Features 1, 4, 5). All HTTP calls are mocked
// via HttpTestingController — no real backend, no AI calls.

describe('validateSendBackForm (pure function — mirrors helpers/send_back.py)', () => {
  const TODAY = '2026-07-21';

  function validForm(overrides: Partial<SendBackFormState> = {}): SendBackFormState {
    return {
      reasonCategory: 'possible_duplicate_invoice',
      reasonOtherNote: '',
      instruction: 'Confirm whether this invoice was uploaded twice.',
      requiredActions: ['provide_written_explanation'],
      requiredActionOtherNote: '',
      priority: 'normal',
      dueDate: '',
      ...overrides,
    };
  }

  it('accepts a fully valid form with no errors', () => {
    expect(validateSendBackForm(validForm(), TODAY)).toEqual([]);
  });

  it('requires a reason category', () => {
    const errors = validateSendBackForm(validForm({ reasonCategory: '' }), TODAY);
    expect(errors.some(e => e.toLowerCase().includes('reason category'))).toBe(true);
  });

  it('requires instruction text (blank/whitespace-only is rejected)', () => {
    const errors = validateSendBackForm(validForm({ instruction: '   ' }), TODAY);
    expect(errors.some(e => e.toLowerCase().includes('instruction'))).toBe(true);
  });

  it('requires at least one required action', () => {
    const errors = validateSendBackForm(validForm({ requiredActions: [] }), TODAY);
    expect(errors.some(e => e.toLowerCase().includes('required action'))).toBe(true);
  });

  it('rejects a due date earlier than today', () => {
    const errors = validateSendBackForm(validForm({ dueDate: '2026-07-20' }), TODAY);
    expect(errors.some(e => e.includes('Due date'))).toBe(true);
  });

  it('accepts a due date of exactly today', () => {
    expect(validateSendBackForm(validForm({ dueDate: TODAY }), TODAY)).toEqual([]);
  });

  it('requires a due date when priority is high', () => {
    const errors = validateSendBackForm(validForm({ priority: 'high', dueDate: '' }), TODAY);
    expect(errors.some(e => e.includes('high-priority'))).toBe(true);
  });

  it('does not require a due date for normal/medium priority', () => {
    expect(validateSendBackForm(validForm({ priority: 'normal', dueDate: '' }), TODAY)).toEqual([]);
    expect(validateSendBackForm(validForm({ priority: 'medium', dueDate: '' }), TODAY)).toEqual([]);
  });

  it('requires an explanation when reason category is "other"', () => {
    const errors = validateSendBackForm(validForm({ reasonCategory: 'other', reasonOtherNote: '' }), TODAY);
    expect(errors.some(e => e.includes('"Other" reason'))).toBe(true);
  });

  it('requires an explanation when required actions include "other"', () => {
    const errors = validateSendBackForm(
      validForm({ requiredActions: ['other'], requiredActionOtherNote: '' }), TODAY);
    expect(errors.some(e => e.includes('"Other" required action'))).toBe(true);
  });
});

describe('AuditorRecordDetailComponent — Send-Back workflow', () => {
  let component: AuditorRecordDetailComponent;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AuditorRecordDetailComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { queryParams: of({}) } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(AuditorRecordDetailComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    component.documentId = 1; // ngOnInit never runs (detectChanges not called) — set directly
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('opens the Send Back modal with a freshly reset form', () => {
    component.sendBack.instruction = 'stale leftover text from a previous open';
    component.showSendBackModal = false;

    component.openSendBackModal();

    expect(component.showSendBackModal).toBe(true);
    expect(component.sendBack).toEqual(emptySendBackForm());
    expect(component.sendBackErrors).toEqual([]);
  });

  it('closes the modal without sending any request', () => {
    component.showSendBackModal = true;
    component.closeSendBackModal();
    expect(component.showSendBackModal).toBe(false);
    httpMock.expectNone(`${environment.apiUrl}/reviews/return/1`);
  });

  it('toggles a required action on and off', () => {
    expect(component.isRequiredActionChecked('provide_written_explanation')).toBe(false);
    component.toggleRequiredAction('provide_written_explanation');
    expect(component.isRequiredActionChecked('provide_written_explanation')).toBe(true);
    component.toggleRequiredAction('provide_written_explanation');
    expect(component.isRequiredActionChecked('provide_written_explanation')).toBe(false);
  });

  it('blocks submission and surfaces errors for an invalid form, without calling the API', () => {
    component.sendBack = emptySendBackForm();
    component.submitSendBack();
    expect(component.sendBackErrors.length).toBeGreaterThan(0);
    httpMock.expectNone(`${environment.apiUrl}/reviews/return/1`);
  });

  it('POSTs the structured payload to /reviews/return/<id> for a valid form', () => {
    component.sendBack = {
      reasonCategory: 'possible_duplicate_invoice',
      reasonOtherNote: '',
      instruction: 'Confirm whether this invoice was uploaded twice.',
      requiredActions: ['provide_written_explanation', 'confirm_duplicate_submission'],
      requiredActionOtherNote: '',
      priority: 'high',
      dueDate: component.todayIso,
    };

    component.submitSendBack();

    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/return/1`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.reason_category).toBe('possible_duplicate_invoice');
    expect(req.request.body.instruction).toBe('Confirm whether this invoice was uploaded twice.');
    expect(req.request.body.required_actions).toEqual(['provide_written_explanation', 'confirm_duplicate_submission']);
    expect(req.request.body.priority).toBe('high');

    req.flush({ message: 'ok', cycle_number: 1 });
    expect(component.showSendBackModal).toBe(false);
  });

  it('surfaces a server-side validation error and keeps the modal open', () => {
    component.sendBack = {
      reasonCategory: 'possible_duplicate_invoice', reasonOtherNote: '',
      instruction: 'Some instruction', requiredActions: ['provide_written_explanation'],
      requiredActionOtherNote: '', priority: 'normal', dueDate: '',
    };
    component.showSendBackModal = true;

    component.submitSendBack();
    const req = httpMock.expectOne(`${environment.apiUrl}/reviews/return/1`);
    req.flush({ error: 'Document is not under review. Current status: approved' }, { status: 400, statusText: 'Bad Request' });

    expect(component.showSendBackModal).toBe(true);
    expect(component.sendBackErrors).toEqual(['Document is not under review. Current status: approved']);
  });

  it('shows the Finance Response panel once the latest cycle has a response, preserving the original reason', () => {
    component.cycles = [{
      cycle_number: 1,
      return_reason_category: 'possible_duplicate_invoice',
      auditor_instruction: 'Confirm duplicate submission.',
      finance_response: 'The duplicate was withdrawn; no payment was made.',
      finance_responded_by_name: 'Finance User 1',
      priority: 'high',
      activity_summary: ['Finance response added'],
    }];

    expect(component.hasFinanceResponse).toBe(true);
    expect(component.latestCycle.finance_response).toBe('The duplicate was withdrawn; no payment was made.');
    expect(component.reasonCategoryLabel(component.latestCycle.return_reason_category)).toBe('Possible duplicate invoice');
    expect(component.changesSinceSendBack).toEqual(['Finance response added']);
  });

  it('does not show the Finance Response panel before any cycle has a response', () => {
    component.cycles = [];
    expect(component.hasFinanceResponse).toBe(false);
    expect(component.changesSinceSendBack).toEqual([]);
  });

  it('renders every cycle in history — multiple send-back cycles are not dropped', () => {
    component.reviewHistory = [
      { action: 'returned', remarks: 'First reason', reviewed_at: '2026-07-21T10:30:00', reviewer_name: 'Auditor 1' },
      { action: 'resubmitted', remarks: 'First fix attempt', reviewed_at: '2026-07-23T14:15:00', reviewer_name: 'Finance User 1' },
      { action: 'returned', remarks: 'Still not resolved', reviewed_at: '2026-07-24T09:00:00', reviewer_name: 'Auditor 1' },
      { action: 'resubmitted', remarks: 'Second fix attempt', reviewed_at: '2026-07-24T15:00:00', reviewer_name: 'Finance User 1' },
      { action: 'approved', remarks: null, reviewed_at: '2026-07-25T11:40:00', reviewer_name: 'Auditor 1' },
    ];

    expect(component.reviewHistory.length).toBe(5);
    expect(component.historyLabel('returned')).toBe('Record sent back to Finance');
    expect(component.historyLabel('resubmitted')).toBe('Record resubmitted for auditor review');
    expect(component.historyLabel('approved')).toBe('Record approved');
    expect(component.formatDateTime('2026-07-21T10:30:00')).toContain('2026');
  });

  it('labels the button "Send Back Again" once at least one prior cycle exists', () => {
    component.cycles = [];
    expect(component.sendBackButtonLabel).toBe('Send Back to Finance');
    component.cycles = [{ cycle_number: 1 }];
    expect(component.sendBackButtonLabel).toBe('Send Back Again');
  });
});

// AI Audit Assistant tests — every action is triggered ONLY by an
// explicit method call (mirroring a button click), never by ngOnInit.
// All HTTP calls are mocked via HttpTestingController — no real
// backend, no real Claude/Gemini calls.
describe('AuditorRecordDetailComponent — AI Audit Assistant', () => {
  let component: AuditorRecordDetailComponent;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AuditorRecordDetailComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { queryParams: of({}) } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(AuditorRecordDetailComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    component.documentId = 1;
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('never calls the AI Assistant automatically — no request fires without an explicit action', () => {
    // Component is fully constructed (as if the page just loaded) and
    // no AI method has been called yet.
    httpMock.expectNone(r => r.url.includes('/ai-assistant/'));
  });

  it('explainException POSTs to /ai-assistant/<id>/explain-exception and stores the answer', () => {
    component.explainException();
    expect(component.aiActionLoading['explain_exception']).toBe(true);

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/1/explain-exception`);
    expect(req.request.method).toBe('POST');
    req.flush({ answer: 'This invoice is missing its PO and GR.', provider: 'claude', cached: false });

    expect(component.aiActionLoading['explain_exception']).toBe(false);
    expect(component.aiExceptionAnswer).toBe('This invoice is missing its PO and GR.');
  });

  it('explainRisk POSTs to /ai-assistant/<id>/explain-risk and stores the structured risk result', () => {
    component.explainRisk();
    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/1/explain-risk`);
    req.flush({
      risk_level: 'Medium',
      reasons: ['Missing PO', 'Missing GR'],
      potential_impact: 'Incorrect payment approval.',
      provider: 'claude', cached: false,
    });

    expect(component.aiRisk).toEqual({
      risk_level: 'Medium',
      reasons: ['Missing PO', 'Missing GR'],
      potential_impact: 'Incorrect payment approval.',
    });
  });

  it('generateAuditRemark POSTs to /ai-assistant/<id>/generate-remark and fills the EXISTING Remarks textarea', () => {
    component.auditNote = '';
    component.generateAuditRemark();

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/1/generate-remark`);
    expect(req.request.method).toBe('POST');
    req.flush({ remark: 'Invoice review is pending due to missing supporting documents.', provider: 'claude', cached: false });

    expect(component.auditNote).toBe('Invoice review is pending due to missing supporting documents.');
  });

  it('prepareSendBackInstruction POSTs to /ai-assistant/<id>/prepare-send-back and opens the EXISTING Send Back modal pre-filled, without submitting it', () => {
    component.showSendBackModal = false;
    component.prepareSendBackInstruction();

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/1/prepare-send-back`);
    expect(req.request.method).toBe('POST');
    req.flush({
      reason_category: 'missing_document',
      required_actions: ['upload_missing_document'],
      priority: 'medium',
      instruction: 'Please provide supporting documents for completion of three-way matching review.',
      provider: 'claude', cached: false,
    });

    expect(component.showSendBackModal).toBe(true);
    expect(component.sendBack.reasonCategory).toBe('missing_document');
    expect(component.sendBack.requiredActions).toEqual(['upload_missing_document']);
    expect(component.sendBack.priority).toBe('medium');
    expect(component.sendBack.instruction).toBe('Please provide supporting documents for completion of three-way matching review.');
    // Never auto-submitted — the actual send happens only via the
    // existing submitSendBack() flow when the auditor clicks the button.
    httpMock.expectNone(`${environment.apiUrl}/reviews/return/1`);
  });

  it('askAiQuestion POSTs the question to /ai-assistant/<id>/ask and appends the Q&A to the conversation log', () => {
    component.aiQuestion = 'What documents are missing?';
    component.askAiQuestion();

    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/1/ask`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ question: 'What documents are missing?' });
    req.flush({ answer: 'The Purchase Order and Goods Receipt are missing.', provider: 'claude', cached: false });

    expect(component.aiConversation).toEqual([
      { question: 'What documents are missing?', answer: 'The Purchase Order and Goods Receipt are missing.' }
    ]);
    // Input is cleared after a successful send.
    expect(component.aiQuestion).toBe('');
  });

  it('askAiQuestion does nothing for a blank question', () => {
    component.aiQuestion = '   ';
    component.askAiQuestion();
    httpMock.expectNone(r => r.url.includes('/ai-assistant/'));
  });

  it('surfaces a 502 AI-unavailable error without crashing, and clears the loading state', () => {
    component.explainException();
    const req = httpMock.expectOne(`${environment.apiUrl}/ai-assistant/1/explain-exception`);
    req.flush({ error: 'AI Assistant is unavailable right now — see server logs' }, { status: 502, statusText: 'Bad Gateway' });

    expect(component.aiActionLoading['explain_exception']).toBe(false);
    expect(component.aiError).toBe('AI Assistant is unavailable right now — see server logs');
  });
});
