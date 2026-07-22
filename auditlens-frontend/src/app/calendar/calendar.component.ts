import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../environments/environment';

// ── Audit Workflow Calendar — shared by both /auditor/calendar and
// /finance/calendar (one component, role read from localStorage, same
// pattern the layouts already use). The backend (GET /calendar/events)
// already scopes which event types come back per role; this component
// only renders whatever it receives and picks role-appropriate
// labels/navigation for the "related action" button. ──

export type EventType =
  | 'pending_review' | 'finance_correction_due' | 'exception_followup'
  | 'anomaly_followup' | 'manual_task';

export type Priority = 'normal' | 'medium' | 'high';

export interface CalendarEvent {
  event_type: EventType;
  date: string; // YYYY-MM-DD
  title: string;
  document_id: number | null;
  invoice_no: string | null;
  vendor_name: string | null;
  priority: Priority;
  description: string | null;
  status: string | null;
  reason_category?: string;
  task_id?: number;
  assigned_to?: number;
  assigned_to_name?: string;
  created_by_name?: string;
}

interface DayCell {
  date: Date;
  iso: string;
  inMonth: boolean;
  isToday: boolean;
  events: CalendarEvent[];
}

export interface TaskFormState {
  title: string;
  description: string;
  date: string;
  assigned_to: string;
  priority: Priority;
}

export function emptyTaskForm(defaultDate: string): TaskFormState {
  return { title: '', description: '', date: defaultDate, assigned_to: '', priority: 'normal' };
}

// Client-side mirror of helpers/calendar_events.py::validate_task_payload
// — instant feedback; the backend re-validates and remains authoritative.
export function validateTaskForm(form: TaskFormState): string[] {
  const errors: string[] = [];
  if (!form.title.trim()) errors.push('Title is required.');
  if (!form.date) errors.push('Date is required.');
  return errors;
}

const EVENT_TYPE_LABELS: Record<EventType, string> = {
  pending_review:          'Pending Review',
  finance_correction_due:  'Finance Correction Due',
  exception_followup:      'Exception Follow-up',
  anomaly_followup:        'Anomaly Follow-up',
  manual_task:             'Manual Task',
};

const EVENT_TYPE_ICONS: Record<EventType, string> = {
  pending_review:          'ph-hourglass-medium',
  finance_correction_due:  'ph-arrow-u-up-left',
  exception_followup:      'ph-warning-circle',
  anomaly_followup:        'ph-sparkle',
  manual_task:             'ph-check-square',
};

@Component({
  selector: 'app-audit-calendar',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './calendar.component.html',
  styleUrls: ['./calendar.component.css']
})
export class CalendarComponent implements OnInit {
  userRole: 'auditor' | 'finance_executive' | '' = '';
  viewMonth: Date = new Date();
  events: CalendarEvent[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';

  weekdayLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  selectedEvent: CalendarEvent | null = null;
  showEventModal: boolean = false;

  selectedDayEvents: CalendarEvent[] = [];
  selectedDayLabel: string = '';
  showDayModal: boolean = false;

  showTaskModal: boolean = false;
  taskForm: TaskFormState = emptyTaskForm(this.toIso(new Date()));
  taskErrors: string[] = [];
  isSubmittingTask: boolean = false;
  assignableUsers: any[] = [];

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      const user = JSON.parse(localStorage.getItem('user') || '{}');
      this.userRole = user?.role || '';
    }
    this.loadEvents();
    this.loadAssignableUsers();
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── Date helpers ─────────────────────────────────────────
  // Deliberately NOT Date.toISOString() — that converts to UTC first,
  // which silently shifts the calendar date by a day for any timezone
  // ahead of UTC (e.g. local midnight in MY/UTC+8 is still "yesterday"
  // in UTC). Every date-to-string conversion in this component goes
  // through local getFullYear/getMonth/getDate instead.

  toIso(d: Date): string {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  private fromIso(iso: string): Date {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d);
  }

  // ── Month navigation + event loading ────────────────────

  get monthLabel(): string {
    return this.viewMonth.toLocaleDateString('en-MY', { month: 'long', year: 'numeric' });
  }

  prevMonth() {
    this.viewMonth = new Date(this.viewMonth.getFullYear(), this.viewMonth.getMonth() - 1, 1);
    this.loadEvents();
  }

  nextMonth() {
    this.viewMonth = new Date(this.viewMonth.getFullYear(), this.viewMonth.getMonth() + 1, 1);
    this.loadEvents();
  }

  goToToday() {
    this.viewMonth = new Date();
    this.loadEvents();
  }

  private monthBounds(): { start: string; end: string } {
    const y = this.viewMonth.getFullYear();
    const m = this.viewMonth.getMonth();
    return {
      start: this.toIso(new Date(y, m, 1)),
      end:   this.toIso(new Date(y, m + 1, 0)),
    };
  }

  loadEvents() {
    this.isLoading = true;
    this.errorMessage = '';
    const { start, end } = this.monthBounds();
    this.http.get<any>(`${this.apiUrl}/calendar/events?start=${start}&end=${end}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.events = res.events || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.events = [];
        this.errorMessage = err.error?.error || 'Failed to load calendar events.';
        this.cdr.detectChanges();
      }
    });
  }

  loadAssignableUsers() {
    this.http.get<any>(`${this.apiUrl}/calendar/assignable-users`, { headers: this.getHeaders() })
      .subscribe({
        next: (res) => { this.assignableUsers = res.users || []; this.cdr.detectChanges(); },
        error: () => { this.assignableUsers = []; }
      });
  }

  // ── Grid ─────────────────────────────────────────────────

  get calendarDays(): DayCell[] {
    const y = this.viewMonth.getFullYear();
    const m = this.viewMonth.getMonth();
    const firstOfMonth = new Date(y, m, 1);
    const startOffset = firstOfMonth.getDay(); // 0 = Sunday
    const todayIso = this.toIso(new Date());

    const days: DayCell[] = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(y, m, 1 - startOffset + i);
      const iso = this.toIso(d);
      days.push({
        date: d,
        iso,
        inMonth: d.getMonth() === m,
        isToday: iso === todayIso,
        events: this.events.filter(e => e.date === iso),
      });
    }
    return days;
  }

  get hasAnyEvents(): boolean {
    return this.events.length > 0;
  }

  visibleChips(day: DayCell): CalendarEvent[] {
    return day.events.slice(0, 3);
  }

  overflowCount(day: DayCell): number {
    return Math.max(0, day.events.length - 3);
  }

  openDay(day: DayCell) {
    if (!day.events.length) return;
    this.selectedDayEvents = day.events;
    this.selectedDayLabel = day.date.toLocaleDateString('en-MY', { day: 'numeric', month: 'long', year: 'numeric' });
    this.showDayModal = true;
  }

  closeDayModal() {
    this.showDayModal = false;
  }

  // ── Event detail ─────────────────────────────────────────

  openEventDetail(event: CalendarEvent, ev?: Event) {
    ev?.stopPropagation();
    this.selectedEvent = event;
    this.showEventModal = true;
    this.showDayModal = false;
  }

  closeEventModal() {
    this.showEventModal = false;
    this.selectedEvent = null;
  }

  eventTypeLabel(type: EventType): string {
    return EVENT_TYPE_LABELS[type] || type;
  }

  eventTypeIcon(type: EventType): string {
    return EVENT_TYPE_ICONS[type] || 'ph-calendar-blank';
  }

  priorityLabel(p: Priority): string {
    if (p === 'high') return 'High';
    if (p === 'medium') return 'Medium';
    return 'Normal';
  }

  priorityClass(p: Priority): string {
    if (p === 'high') return 'priority-high';
    if (p === 'medium') return 'priority-medium';
    return 'priority-normal';
  }

  formatEventDate(iso: string): string {
    if (!iso) return '-';
    return this.fromIso(iso).toLocaleDateString('en-MY', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  // ── Related action (Event Detail panel) ─────────────────
  // Deliberately role-aware: a finance user is never routed into an
  // /auditor/* page (there's no route guard in this app — the API
  // itself is what actually enforces role access — so sending Finance
  // to an auditor-only page would just render a 403, a broken
  // experience worth avoiding at the navigation layer too).

  relatedActionLabel(event: CalendarEvent): string {
    if (event.event_type === 'manual_task') {
      return event.status === 'done' ? '' : 'Mark Complete';
    }
    if (event.event_type === 'finance_correction_due') {
      return this.userRole === 'finance_executive' ? 'Correct Document' : 'Review Correction';
    }
    if (event.event_type === 'exception_followup' || event.event_type === 'anomaly_followup') {
      return 'View Exception';
    }
    return 'Open Document';
  }

  runRelatedAction(event: CalendarEvent) {
    if (event.event_type === 'manual_task') {
      if (event.task_id) this.completeTask(event.task_id);
      return;
    }
    if (event.event_type === 'finance_correction_due' && this.userRole === 'finance_executive') {
      this.router.navigate(['/finance/ocr-review']);
      return;
    }
    if (event.document_id) {
      this.router.navigate(['/auditor/record-detail'], { queryParams: { document_id: event.document_id } });
    }
  }

  completeTask(taskId: number) {
    this.http.patch<any>(`${this.apiUrl}/calendar/tasks/${taskId}/complete`, {}, { headers: this.getHeaders() })
      .subscribe({
        next: () => {
          this.closeEventModal();
          this.loadEvents();
        },
        error: (err) => {
          this.errorMessage = err.error?.error || 'Failed to complete task.';
          this.cdr.detectChanges();
        }
      });
  }

  // ── Create Manual Task ───────────────────────────────────

  openTaskModal(prefillIso?: string) {
    this.taskForm = emptyTaskForm(prefillIso || this.toIso(new Date()));
    this.taskErrors = [];
    this.showTaskModal = true;
  }

  closeTaskModal() {
    this.showTaskModal = false;
  }

  submitTask() {
    if (this.isSubmittingTask) return;
    const errors = validateTaskForm(this.taskForm);
    if (errors.length) {
      this.taskErrors = errors;
      this.cdr.detectChanges();
      return;
    }
    this.taskErrors = [];
    this.isSubmittingTask = true;

    const payload: any = {
      title:    this.taskForm.title.trim(),
      date:     this.taskForm.date,
      priority: this.taskForm.priority,
    };
    if (this.taskForm.description.trim()) payload.description = this.taskForm.description.trim();
    if (this.taskForm.assigned_to) payload.assigned_to = this.taskForm.assigned_to;

    this.http.post<any>(`${this.apiUrl}/calendar/tasks`, payload, { headers: this.getHeaders() })
      .subscribe({
        next: () => {
          this.isSubmittingTask = false;
          this.showTaskModal = false;
          this.loadEvents();
        },
        error: (err) => {
          this.isSubmittingTask = false;
          this.taskErrors = [err.error?.error || 'Failed to create task.'];
          this.cdr.detectChanges();
        }
      });
  }
}
