import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../environments/environment';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './login.component.html',
  styleUrls: ['./login.component.css']
})
export class LoginComponent {
  email: string = '';
  password: string = '';
  showPassword: boolean = false;
  isLoading: boolean = false;
  errorMessage: string = '';

  private apiUrl = environment.apiUrl;

  pipelineStages = [
    { label: 'Document Upload', icon: 'ph-cloud-arrow-up' },
    { label: 'AI OCR Extraction', icon: 'ph-sparkle' },
    { label: 'Validation & Three-way Matching', icon: 'ph-arrows-clockwise' },
    { label: 'Risk & Anomaly Detection', icon: 'ph-shield-warning' },
    { label: 'AI Audit Assistant', icon: 'ph-chat-circle-dots' },
    { label: 'Auditor Decision', icon: 'ph-user-check' }
  ];

  capabilities = [
    { title: 'AI Document Analysis', desc: 'Extracts and understands invoices, POs, and receipts.', icon: 'ph-magnifying-glass' },
    { title: 'Three-way Matching', desc: 'Reconciles invoices, purchase orders, and goods receipts.', icon: 'ph-arrows-clockwise' },
    { title: 'Risk & Exception Detection', desc: 'Flags anomalies before they reach the ledger.', icon: 'ph-shield-warning' },
    { title: 'AI Audit Assistant', desc: 'Answers questions grounded in the audit trail.', icon: 'ph-chat-circle-dots' },
    { title: 'Authenticity Verification', desc: 'Checks document credibility and signs of tampering.', icon: 'ph-shield-check' },
    { title: 'Approval Intelligence', desc: 'Surfaces evidence and risk to support auditor decisions.', icon: 'ph-check-circle' }
  ];

  constructor(private http: HttpClient, private router: Router) {}

  togglePassword() {
    this.showPassword = !this.showPassword;
  }

  onLogin() {
    this.errorMessage = '';

    if (!this.email || !this.password) {
      this.errorMessage = 'Please enter your email and password.';
      return;
    }

    this.isLoading = true;

    this.http.post<any>(`${this.apiUrl}/auth/login`, {
      email: this.email,
      password: this.password
    }).subscribe({
      next: (res) => {
        this.isLoading = false;
        localStorage.setItem('access_token', res.access_token);
        localStorage.setItem('user', JSON.stringify(res.user));

        const role = res.user.role;
        if (role === 'finance_executive') {
          this.router.navigate(['/finance/home']);
        } else if (role === 'auditor') {
          this.router.navigate(['/auditor/home']);
        } else if (role === 'admin') {
          this.router.navigate(['/admin/home']);
        }
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Login failed. Please try again.';
      }
    });
  }

  goToRegister() {
    this.router.navigate(['/register']);
  }
}
