import { Component, ElementRef, HostListener } from '@angular/core';
import { CommonModule } from '@angular/common';

interface MockNotification {
  icon: string;
  colorClass: string;
  title: string;
}

// Frontend-only notification bell shared across every Finance page
// header. No backend notification system exists yet — the list below
// is static mock data, per this task's explicit "frontend only, no
// backend" scope. Kept as one shared component (rather than copied
// per page, unlike this app's usual per-component-copy convention for
// small label lookups) because this is a genuinely stateful, reusable
// interactive widget used identically across 7 pages.
@Component({
  selector: 'app-finance-notification-bell',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './finance-notification-bell.component.html',
  styleUrls: ['./finance-notification-bell.component.css']
})
export class FinanceNotificationBellComponent {
  isOpen = false;

  notifications: MockNotification[] = [
    { icon: 'ph-arrow-u-up-left', colorClass: 'notif-danger', title: 'Invoice returned for correction' },
    { icon: 'ph-scan', colorClass: 'notif-warning', title: 'OCR review required' },
    { icon: 'ph-check-circle', colorClass: 'notif-success', title: 'Document approved' },
  ];

  constructor(private elementRef: ElementRef) {}

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
}
