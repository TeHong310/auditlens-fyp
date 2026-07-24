import { Component, ElementRef, HostListener, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FinanceNotificationService, WorkflowNotification } from './finance-notification.service';

@Component({
  selector: 'app-finance-notification-bell',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './finance-notification-bell.component.html',
  styleUrls: ['./finance-notification-bell.component.css']
})
export class FinanceNotificationBellComponent implements OnInit {
  isOpen = false;
  notifications: WorkflowNotification[] = [];

  constructor(
    private elementRef: ElementRef,
    private notificationService: FinanceNotificationService
  ) {}

  ngOnInit() {
    // See FinanceNotificationService — mock data today, same
    // Observable<WorkflowNotification[]> shape a real API call would
    // return, so no change needed here once a backend endpoint exists.
    this.notificationService.getNotifications().subscribe(list => {
      this.notifications = list;
    });
  }

  toggle(event: Event) {
    event.stopPropagation();
    this.isOpen = !this.isOpen;
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event) {
    if (this.isOpen && !this.elementRef.nativeElement.contains(event.target)) {
      this.isOpen = false;
    }
  }

  relativeTime(dateStr: string): string {
    if (!dateStr) return '-';
    const diffMs = Date.now() - new Date(dateStr).getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr} hr ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay === 1) return '1 day ago';
    return `${diffDay} days ago`;
  }
}
