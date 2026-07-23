import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, RouterModule, RouterOutlet } from '@angular/router';

// Authentication Phase — minimal Admin layout, mirroring the existing
// auditor/finance layout pattern exactly (sidebar, user info, logout).
// A placeholder shell only: routes/admin.py already has a full set of
// admin endpoints (users, documents, exceptions, audit logs,
// statistics) with no frontend consumer yet — out of scope for this
// auth-flow phase, which only needs a real page for role === 'admin'
// to land on after login instead of a 404.
@Component({
  selector: 'app-admin-layout',
  standalone: true,
  imports: [CommonModule, RouterModule, RouterOutlet],
  templateUrl: './admin-layout.component.html',
  styleUrls: ['./admin-layout.component.css']
})
export class AdminLayoutComponent implements OnInit {
  user: any = {};
  showUserMenu: boolean = false;
  isSidebarOpen = false;

  navItems = [
    { label: 'Dashboard', icon: 'home', route: '/admin/home' },
    { label: 'User Management', icon: 'users', route: '/admin/users' },
    { label: 'Document Management', icon: 'documents', route: '/admin/documents' },
  ];

  constructor(private router: Router) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      this.user = JSON.parse(localStorage.getItem('user') || '{}');
    }
  }

  isActive(route: string): boolean {
    return this.router.url.startsWith(route);
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
    return this.user?.full_name?.charAt(0).toUpperCase() || 'A';
  }
}
