import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { Router } from '@angular/router';

import { CalendarComponent, validateTaskForm, emptyTaskForm, TaskFormState } from './calendar.component';
import { environment } from '../../environments/environment';

// Audit Workflow Calendar tests. All HTTP calls are mocked via
// HttpTestingController — no real backend, no AI calls.

describe('validateTaskForm (pure function — mirrors helpers/calendar_events.py)', () => {
  function validForm(overrides: Partial<TaskFormState> = {}): TaskFormState {
    return { title: 'Follow up with vendor', description: '', date: '2026-07-25', assigned_to: '', priority: 'normal', ...overrides };
  }

  it('accepts a fully valid form with no errors', () => {
    expect(validateTaskForm(validForm())).toEqual([]);
  });

  it('requires a title', () => {
    const errors = validateTaskForm(validForm({ title: '   ' }));
    expect(errors.some(e => e.toLowerCase().includes('title'))).toBe(true);
  });

  it('requires a date', () => {
    const errors = validateTaskForm(validForm({ date: '' }));
    expect(errors.some(e => e.toLowerCase().includes('date'))).toBe(true);
  });

  it('emptyTaskForm seeds the given date and defaults priority to normal', () => {
    const form = emptyTaskForm('2026-08-01');
    expect(form.date).toBe('2026-08-01');
    expect(form.priority).toBe('normal');
    expect(form.title).toBe('');
  });
});

describe('CalendarComponent', () => {
  let component: CalendarComponent;
  let httpMock: HttpTestingController;
  let navigateCalls: any[][];

  beforeEach(async () => {
    navigateCalls = [];
    await TestBed.configureTestingModule({
      imports: [CalendarComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Router, useValue: { navigate: (...args: any[]) => { navigateCalls.push(args); return Promise.resolve(true); } } },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(CalendarComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    // ngOnInit is never triggered (detectChanges() not called), so no
    // automatic HTTP calls happen — each test drives state directly.
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('toIso formats a local date without any UTC/timezone shift', () => {
    // Regression guard: Date.toISOString() would shift this date by a
    // day in any timezone ahead of UTC — toIso() must not do that.
    const d = new Date(2026, 6, 1); // 1 July 2026, local midnight
    expect(component.toIso(d)).toBe('2026-07-01');
  });

  it('calendarDays produces a 42-cell grid with the 1st of the month correctly flagged inMonth', () => {
    component.viewMonth = new Date(2026, 6, 1); // July 2026
    const days = component.calendarDays;
    expect(days.length).toBe(42);
    const first = days.find(d => d.iso === '2026-07-01');
    expect(first?.inMonth).toBe(true);
    const last = days.find(d => d.iso === '2026-07-31');
    expect(last?.inMonth).toBe(true);
  });

  it('loadEvents requests /calendar/events with the current month\'s start/end bounds', () => {
    component.viewMonth = new Date(2026, 6, 15); // any day in July 2026
    component.loadEvents();

    const req = httpMock.expectOne(`${environment.apiUrl}/calendar/events?start=2026-07-01&end=2026-07-31`);
    expect(req.request.method).toBe('GET');
    req.flush({ total: 0, events: [] });

    expect(component.events).toEqual([]);
    expect(component.hasAnyEvents).toBe(false);
  });

  it('groups loaded events onto the matching day cell by date', () => {
    component.viewMonth = new Date(2026, 6, 1);
    component.events = [
      { event_type: 'pending_review', date: '2026-07-05', title: 'Review IX1', document_id: 1,
        invoice_no: 'IX1', vendor_name: 'Acme', priority: 'normal', description: null, status: 'under_review' },
    ] as any;

    const day = component.calendarDays.find(d => d.iso === '2026-07-05');
    expect(day?.events.length).toBe(1);
    expect(day?.events[0].title).toBe('Review IX1');
  });

  it('submitTask blocks an invalid form and never calls the API', () => {
    component.taskForm = emptyTaskForm('');
    component.taskForm.title = '';
    component.submitTask();
    expect(component.taskErrors.length).toBeGreaterThan(0);
    httpMock.expectNone(`${environment.apiUrl}/calendar/tasks`);
  });

  it('submitTask POSTs the task payload for a valid form and closes the modal on success', () => {
    component.taskForm = { title: 'Chase vendor', description: 'Needs a callback', date: '2026-08-01', assigned_to: '', priority: 'high' };
    component.showTaskModal = true;

    component.submitTask();
    const req = httpMock.expectOne(`${environment.apiUrl}/calendar/tasks`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ title: 'Chase vendor', date: '2026-08-01', priority: 'high', description: 'Needs a callback' });
    req.flush({ message: 'ok', task_id: 1 });

    // loadEvents() fires after a successful create — flush it too.
    const eventsReq = httpMock.expectOne(r => r.url.startsWith(`${environment.apiUrl}/calendar/events`));
    eventsReq.flush({ total: 0, events: [] });

    expect(component.showTaskModal).toBe(false);
  });

  it('completeTask PATCHes the task and refreshes events', () => {
    component.selectedEvent = { event_type: 'manual_task', task_id: 9 } as any;
    component.showEventModal = true;

    component.completeTask(9);
    const req = httpMock.expectOne(`${environment.apiUrl}/calendar/tasks/9/complete`);
    expect(req.request.method).toBe('PATCH');
    req.flush({ message: 'ok' });

    const eventsReq = httpMock.expectOne(r => r.url.startsWith(`${environment.apiUrl}/calendar/events`));
    eventsReq.flush({ total: 0, events: [] });

    expect(component.showEventModal).toBe(false);
  });

  it('relatedActionLabel and runRelatedAction respect role for finance_correction_due', () => {
    const event = { event_type: 'finance_correction_due', document_id: 5 } as any;

    component.userRole = 'finance_executive';
    expect(component.relatedActionLabel(event)).toBe('Correct Document');
    component.runRelatedAction(event);
    // finance_executive is routed to the existing OCR review page —
    // never into an /auditor/* page, since there's no route guard and
    // the API itself would just 403 a finance user there.
    expect(navigateCalls[0][0]).toEqual(['/finance/ocr-review']);

    navigateCalls = [];
    component.userRole = 'auditor';
    expect(component.relatedActionLabel(event)).toBe('Review Correction');
    component.runRelatedAction(event);
    expect(navigateCalls[0][0]).toEqual(['/auditor/record-detail']);
    expect(navigateCalls[0][1]).toEqual({ queryParams: { document_id: 5 } });
  });

  it('manual_task with status "done" has no related action (already complete)', () => {
    const event = { event_type: 'manual_task', status: 'done' } as any;
    expect(component.relatedActionLabel(event)).toBe('');
  });
});
