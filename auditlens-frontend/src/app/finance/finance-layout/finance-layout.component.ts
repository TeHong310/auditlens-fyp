import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, RouterModule, RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-finance-layout',
  standalone: true,
  imports: [CommonModule, RouterModule, RouterOutlet],
  templateUrl: './finance-layout.component.html',
  styleUrls: ['./finance-layout.component.css']
})
export class FinanceLayoutComponent implements OnInit {
  user: any = {};
  showUserMenu: boolean = false;
  isSidebarOpen = false;

  navItems = [
    { label: 'Finance Home', icon: 'home', route: '/finance/home' },
    { label: 'Upload Document', icon: 'upload', route: '/finance/upload' },
    { label: 'OCR Review', icon: 'review', route: '/finance/ocr-review' },
    { label: 'Calendar', icon: 'calendar', route: '/finance/calendar' },
    { label: 'Report', icon: 'report', route: '/finance/report' },
  ];

  constructor(private router: Router) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      this.user = JSON.parse(localStorage.getItem('user') || '{}');
    }
  }

  isActive(route: string): boolean {
    return this.router.url === route;
  }

  toggleUserMenu() {
    this.showUserMenu = !this.showUserMenu;
  }

  toggleSidebar() {
    this.isSidebarOpen = !this.isSidebarOpen;
  }

  closeSidebar() {
    this.isSidebarOpen = false;
  }

  logout() {
    if (typeof window !== 'undefined') {
      localStorage.removeItem('access_token');
      localStorage.removeItem('user');
    }
    this.router.navigate(['/login']);
  }

  getInitial(): string {
    return this.user?.full_name?.charAt(0).toUpperCase() || 'F';
  }
}