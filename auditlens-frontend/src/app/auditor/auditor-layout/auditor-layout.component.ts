import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router, RouterModule, RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-auditor-layout',
  standalone: true,
  imports: [CommonModule, RouterModule, RouterOutlet],
  templateUrl: './auditor-layout.component.html',
  styleUrls: ['./auditor-layout.component.css']
})
export class AuditorLayoutComponent implements OnInit {
  user: any = {};
  showUserMenu: boolean = false;

  navItems = [
    { label: 'Auditor Home',  icon: 'home',       route: '/auditor/home' },
    { label: 'Review Queue',  icon: 'review',      route: '/auditor/review-queue' },
    { label: 'Record Detail', icon: 'record',      route: '/auditor/record-detail' },
    { label: 'Exceptions',    icon: 'exceptions',  route: '/auditor/exceptions' },
    { label: 'Report & Log',  icon: 'report',      route: '/auditor/report' },
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