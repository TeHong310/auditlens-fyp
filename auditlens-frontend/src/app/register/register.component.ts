import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../environments/environment';

@Component({
  selector: 'app-register',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './register.component.html',
  styleUrls: ['./register.component.css']
})
export class RegisterComponent {
  fullName: string = '';
  email: string = '';
  password: string = '';
  confirmPassword: string = '';
  role: string = '';
  agreeTerms: boolean = false;
  showPassword: boolean = false;
  showConfirmPassword: boolean = false;
  isLoading: boolean = false;
  errorMessage: string = '';
  successMessage: string = '';

  roles = [
    { value: 'finance_executive', label: 'Finance Executive' },
    { value: 'auditor', label: 'Auditor' },
    { value: 'admin', label: 'Admin' }
  ];

  private apiUrl = environment.apiUrl;

  constructor(private http: HttpClient, private router: Router) {}

  togglePassword() { this.showPassword = !this.showPassword; }
  toggleConfirmPassword() { this.showConfirmPassword = !this.showConfirmPassword; }

  onRegister() {
    this.errorMessage = '';
    this.successMessage = '';

    if (!this.fullName || !this.email || !this.password || !this.confirmPassword || !this.role) {
      this.errorMessage = 'Please fill in all required fields.';
      return;
    }

    if (this.password !== this.confirmPassword) {
      this.errorMessage = 'Passwords do not match.';
      return;
    }

    if (this.password.length < 6) {
      this.errorMessage = 'Password must be at least 6 characters.';
      return;
    }

    if (!this.agreeTerms) {
      this.errorMessage = 'Please agree to the terms and privacy policy.';
      return;
    }

    this.isLoading = true;

    this.http.post<any>(`${this.apiUrl}/auth/register`, {
      full_name: this.fullName,
      email: this.email,
      password: this.password,
      role: this.role
    }).subscribe({
      next: (res) => {
        this.isLoading = false;
        this.router.navigate(['/register-success']);
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Registration failed. Please try again.';
      }
    });
  }

  goToLogin() {
    this.router.navigate(['/login']);
  }
}