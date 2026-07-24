import { Injectable } from '@angular/core';
import { Observable, of } from 'rxjs';

export type NotificationType = 'correction' | 'ocr_review' | 'approved';

export interface WorkflowNotification {
  id: number;
  type: NotificationType;
  icon: string;
  colorClass: string;
  documentRef: string;
  message: string;
  reason?: string;
  timestamp: string;
}

// Mock, realistic workflow-event notifications, shaped after this
// app's real audit workflow (send-back cycles, OCR review queue,
// approvals) — no notifications table/API exists in the backend yet
// (confirmed by inspection), so this is illustrative data only.
function buildMockNotifications(): WorkflowNotification[] {
  const now = Date.now();
  return [
    {
      id: 1,
      type: 'correction',
      icon: 'ph-arrow-u-up-left',
      colorClass: 'notif-danger',
      documentRef: 'INV-1042',
      message: 'Auditor sent back invoice for correction',
      reason: 'Missing purchase order reference',
      timestamp: new Date(now - 2 * 60 * 60 * 1000).toISOString(),
    },
    {
      id: 2,
      type: 'ocr_review',
      icon: 'ph-scan',
      colorClass: 'notif-warning',
      documentRef: 'INV-1039',
      message: 'OCR review required',
      reason: 'Low extraction confidence (62%)',
      timestamp: new Date(now - 5 * 60 * 60 * 1000).toISOString(),
    },
    {
      id: 3,
      type: 'approved',
      icon: 'ph-check-circle',
      colorClass: 'notif-success',
      documentRef: 'INV-1031',
      message: 'Document approved',
      timestamp: new Date(now - 26 * 60 * 60 * 1000).toISOString(),
    },
  ];
}

// Frontend-only notification source for the Finance header bell.
// Deliberately shaped as an injectable service returning an
// Observable<WorkflowNotification[]> — the same shape a real HTTP
// call would return — so connecting a real backend later is a
// one-line change inside getNotifications() (swap the `of(...)` body
// for `this.http.get<WorkflowNotification[]>(`${apiUrl}/notifications`)`)
// with no change required in any component that consumes this service.
@Injectable({ providedIn: 'root' })
export class FinanceNotificationService {
  getNotifications(): Observable<WorkflowNotification[]> {
    return of(buildMockNotifications());
  }
}
