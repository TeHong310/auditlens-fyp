import { Component, ElementRef, HostListener, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';

// Header avatar button + dropdown menu, shared across every Finance
// page. Logout mirrors finance-layout.component.ts's own logout()
// exactly (same localStorage keys, same redirect) — this is a second,
// additional entry point for it (the top-right header), not a
// replacement for the sidebar's existing one.
@Component({
  selector: 'app-finance-user-menu',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './finance-user-menu.component.html',
  styleUrls: ['./finance-user-menu.component.css']
})
export class FinanceUserMenuComponent implements OnInit {
  isOpen = false;
  user: any = {};

  constructor(private router: Router, private elementRef: ElementRef) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      this.user = JSON.parse(localStorage.getItem('user') || '{}');
    }
  }

  getInitial(): string {
    return this.user?.full_name?.charAt(0).toUpperCase() || 'F';
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

  goToProfile() {
    this.isOpen = false;
    this.router.navigate(['/finance/profile']);
  }

  logout() {
    if (typeof window !== 'undefined') {
      localStorage.removeItem('access_token');
      localStorage.removeItem('user');
    }
    this.router.navigate(['/login']);
  }
}
