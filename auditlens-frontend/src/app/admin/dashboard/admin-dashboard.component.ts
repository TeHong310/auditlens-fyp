import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';

// Authentication Phase — placeholder Admin dashboard. Reads the
// EXISTING, unmodified GET /admin/statistics endpoint (routes/
// admin.py, already built, just never had a frontend consumer) — no
// new backend calculation, purely a landing page so role === 'admin'
// has somewhere real to go after login.
@Component({
  selector: 'app-admin-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './admin-dashboard.component.html',
  styleUrls: ['./admin-dashboard.component.css']
})
export class AdminDashboardComponent implements OnInit {
  user: any = {};
  isLoading: boolean = false;
  errorMessage: string = '';
  stats: any = null;

  private apiUrl = environment.apiUrl;

  constructor(private http: HttpClient, private cdr: ChangeDetectorRef) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      this.user = JSON.parse(localStorage.getItem('user') || '{}');
    }
    this.loadStatistics();
  }

  private getHeaders(): HttpHeaders {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  loadStatistics() {
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/admin/statistics`, { headers: this.getHeaders() }).subscribe({
      next: (res) => {
        this.stats = res;
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load system statistics.';
        this.cdr.detectChanges();
      }
    });
  }
}
