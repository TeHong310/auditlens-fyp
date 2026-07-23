import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

@Component({
  selector: 'app-admin-users',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-users.component.html',
  styleUrls: ['./admin-users.component.css']
})
export class AdminUsersComponent implements OnInit {
  users: any[] = [];
  isLoading: boolean = false;
  errorMessage: string = '';
  successMessage: string = '';

  currentUserId: number | null = null;

  userPendingDelete: any = null;
  isDeleting: boolean = false;
  deleteError: string = '';

  userPendingReset: any = null;
  newPassword: string = '';
  isResetting: boolean = false;
  resetError: string = '';

  private apiUrl = environment.apiUrl;

  constructor(private http: HttpClient, private cdr: ChangeDetectorRef) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      const u = JSON.parse(localStorage.getItem('user') || '{}');
      this.currentUserId = u?.user_id ?? null;
    }
    this.loadUsers();
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadUsers() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/admin/users`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.users = res.users || [];
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load users.';
        this.cdr.detectChanges();
      }
    });
  }

  roleLabel(role: string): string {
    if (role === 'finance_executive') return 'Finance Executive';
    if (role === 'auditor') return 'Auditor';
    if (role === 'admin') return 'Admin';
    return role;
  }

  roleBadgeClass(role: string): string {
    if (role === 'admin') return 'badge-admin';
    if (role === 'auditor') return 'badge-auditor';
    return 'badge-finance';
  }

  private flashSuccess(msg: string) {
    this.successMessage = msg;
    this.cdr.detectChanges();
    setTimeout(() => {
      this.successMessage = '';
      this.cdr.detectChanges();
    }, 3000);
  }

  toggleStatus(u: any) {
    const newStatus = !u.is_active;
    this.errorMessage = '';
    this.http.put<any>(`${this.apiUrl}/admin/users/${u.user_id}/status`,
      { is_active: newStatus }, { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        u.is_active = newStatus;
        this.flashSuccess(`User ${newStatus ? 'enabled' : 'disabled'} successfully.`);
      },
      error: (err) => {
        this.errorMessage = err.error?.error || 'Failed to update user status.';
        this.cdr.detectChanges();
      }
    });
  }

  openDeleteModal(u: any) {
    this.userPendingDelete = u;
    this.deleteError = '';
  }

  cancelDelete() {
    if (this.isDeleting) return;
    this.userPendingDelete = null;
    this.deleteError = '';
  }

  confirmDelete() {
    if (!this.userPendingDelete || this.isDeleting) return;
    this.isDeleting = true;
    this.deleteError = '';
    const id = this.userPendingDelete.user_id;
    this.http.delete<any>(`${this.apiUrl}/admin/users/${id}`, { headers: this.getHeaders() }).subscribe({
      next: () => {
        this.isDeleting = false;
        this.users = this.users.filter(x => x.user_id !== id);
        this.userPendingDelete = null;
        this.flashSuccess('User deleted successfully.');
      },
      error: (err) => {
        this.isDeleting = false;
        this.deleteError = err.error?.error || 'Failed to delete user.';
        this.cdr.detectChanges();
      }
    });
  }

  openResetModal(u: any) {
    this.userPendingReset = u;
    this.newPassword = '';
    this.resetError = '';
  }

  cancelReset() {
    if (this.isResetting) return;
    this.userPendingReset = null;
    this.newPassword = '';
    this.resetError = '';
  }

  confirmReset() {
    if (!this.userPendingReset || this.isResetting) return;
    if (!this.newPassword || this.newPassword.length < 6) {
      this.resetError = 'Password must be at least 6 characters.';
      return;
    }
    this.isResetting = true;
    this.resetError = '';
    const id = this.userPendingReset.user_id;
    this.http.post<any>(`${this.apiUrl}/admin/users/${id}/reset-password`,
      { new_password: this.newPassword }, { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isResetting = false;
        this.userPendingReset = null;
        this.newPassword = '';
        this.flashSuccess('Password reset successfully.');
      },
      error: (err) => {
        this.isResetting = false;
        this.resetError = err.error?.error || 'Failed to reset password.';
        this.cdr.detectChanges();
      }
    });
  }
}
