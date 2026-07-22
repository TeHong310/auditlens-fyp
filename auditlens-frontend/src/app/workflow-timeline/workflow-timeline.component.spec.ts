import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';

import { WorkflowTimelineComponent } from './workflow-timeline.component';
import { environment } from '../../environments/environment';

// Document Workflow Timeline tests. All HTTP calls are mocked via
// HttpTestingController — no real backend, no AI calls.
describe('WorkflowTimelineComponent', () => {
  let component: WorkflowTimelineComponent;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [WorkflowTimelineComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(WorkflowTimelineComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('does not call the API until documentId is set', () => {
    httpMock.expectNone(r => r.url.includes('/documents/') && r.url.includes('/timeline'));
  });

  it('ngOnChanges triggers loadTimeline() when documentId changes to a real value', () => {
    component.documentId = 42;
    component.ngOnChanges({
      documentId: { currentValue: 42, previousValue: null, firstChange: true, isFirstChange: () => true }
    } as any);

    const req = httpMock.expectOne(`${environment.apiUrl}/documents/42/timeline`);
    expect(req.request.method).toBe('GET');
    req.flush({ document_id: 42, events: [{ event: 'document_uploaded', label: 'Document Uploaded', status: 'completed', detail: null, timestamp: '2026-07-22T09:10:00' }] });

    expect(component.events.length).toBe(1);
    expect(component.isLoading).toBe(false);
  });

  it('loadTimeline stores the returned events in order', () => {
    component.documentId = 7;
    component.loadTimeline();

    const req = httpMock.expectOne(`${environment.apiUrl}/documents/7/timeline`);
    req.flush({
      document_id: 7,
      events: [
        { event: 'document_uploaded', label: 'Document Uploaded', status: 'completed', detail: null, timestamp: '2026-07-22T09:10:00' },
        { event: 'ocr_extraction', label: 'OCR Extraction', status: 'completed', detail: 'Confidence: 96.23%', timestamp: null },
        { event: 'three_way_matching', label: 'Three-way Matching', status: 'action_required', detail: 'Missing: Purchase Order', timestamp: null },
      ]
    });

    expect(component.events.length).toBe(3);
    expect(component.events[2].status).toBe('action_required');
  });

  it('surfaces a server-side error without crashing', () => {
    component.documentId = 9;
    component.loadTimeline();

    const req = httpMock.expectOne(`${environment.apiUrl}/documents/9/timeline`);
    req.flush({ error: 'Access denied' }, { status: 403, statusText: 'Forbidden' });

    expect(component.isLoading).toBe(false);
    expect(component.errorMessage).toBe('Access denied');
    expect(component.events).toEqual([]);
  });

  it('loadTimeline does nothing when documentId is null', () => {
    component.documentId = null;
    component.loadTimeline();
    httpMock.expectNone(r => r.url.includes('/timeline'));
  });

  it('maps each status to exactly the icon/class the feature specifies', () => {
    expect(component.statusIcon('completed')).toBe('ph-check-circle');
    expect(component.statusIcon('action_required')).toBe('ph-warning');
    expect(component.statusIcon('pending')).toBe('ph-circle');

    expect(component.statusClass('completed')).toBe('wt-completed');
    expect(component.statusClass('action_required')).toBe('wt-action-required');
    expect(component.statusClass('pending')).toBe('wt-pending');
  });
});
