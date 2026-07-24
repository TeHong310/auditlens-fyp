import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { FinanceNotificationBellComponent } from '../shared/finance-notification-bell.component';
import { FinanceUserMenuComponent } from '../shared/finance-user-menu.component';

const ROLE_LABELS: Record<string, string> = {
  finance_executive: 'Finance Executive',
  auditor: 'Auditor',
  admin: 'Administrator',
};

// Profile Settings page. No self-service profile-update or change-
// password backend endpoint exists yet (GET /auth/me is read-only;
// the only password-reset route is admin-only, for resetting OTHER
// users). Both forms below are fully built and validated client-side,
// but submission is intentionally NOT faked as a persisted success -
// each shows a clear "not yet connected to a backend endpoint"
// notice, so nothing here misleads a user into thinking their
// password actually changed when it didn't.
@Component({
  selector: 'app-finance-profile',
  standalone: true,
  imports: [CommonModule, FormsModule, FinanceNotificationBellComponent, FinanceUserMenuComponent],
  templateUrl: './finance-profile.component.html',
  styleUrls: ['./finance-profile.component.css']
})
export class FinanceProfileComponent implements OnInit {
  user: any = {};

  fullName = '';
  email = '';
  profileNote = '';

  currentPassword = '';
  newPassword = '';
  confirmPassword = '';
  passwordError = '';
  passwordNote = '';

  constructor(private router: Router) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      this.user = JSON.parse(localStorage.getItem('user') || '{}');
    }
    this.fullName = this.user?.full_name || '';
    this.email = this.user?.email || '';
  }

  getInitial(): string {
    return this.user?.full_name?.charAt(0).toUpperCase() || 'F';
  }

  roleLabel(): string {
    return ROLE_LABELS[this.user?.role] || this.user?.role || 'User';
  }

  saveProfile() {
    this.profileNote = 'Profile updates aren’t connected to a backend endpoint yet — this form is ready to wire up once one exists.';
  }

  changePassword() {
    this.passwordError = '';
    this.passwordNote = '';

    if (!this.currentPassword || !this.newPassword || !this.confirmPassword) {
      this.passwordError = 'Please fill in all password fields.';
      return;
    }
    if (this.newPassword.length < 6) {
      this.passwordError = 'New password must be at least 6 characters.';
      return;
    }
    if (this.newPassword !== this.confirmPassword) {
      this.passwordError = 'New password and confirmation do not match.';
      return;
    }

    this.passwordNote = 'Password changes aren’t connected to a backend endpoint yet — this form is ready to wire up once one exists.';
  }

  goBack() {
    this.router.navigate(['/finance/home']);
  }
}
