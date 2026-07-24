import { Component, ElementRef, OnInit, Renderer2, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { environment } from '../../environments/environment';

interface Particle {
  left: number;
  top: number;
  size: number;
  duration: number;
  delay: number;
}

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './login.component.html',
  styleUrls: ['./login.component.css']
})
export class LoginComponent implements OnInit {
  @ViewChild('pageWrapper') pageWrapperRef!: ElementRef<HTMLElement>;

  email: string = '';
  password: string = '';
  showPassword: boolean = false;
  isLoading: boolean = false;
  errorMessage: string = '';

  // Ambient background particles — a small, fixed set (not a dense
  // field) generated once at load, purely for the *ngFor below. Actual
  // drifting motion is pure CSS (@keyframes particleDrift); this array
  // only supplies each dot's position/size/timing so they don't all
  // move in lockstep.
  particles: Particle[] = [];

  // Mouse parallax — the one piece of decorative JS this redesign adds.
  // Written directly via Renderer2 (bypasses Angular change detection)
  // and throttled to one update per animation frame, so it stays cheap
  // even on fast mousemove. Disabled entirely under prefers-reduced-
  // motion. Purely cosmetic: reads no form state, writes nothing but
  // two CSS custom properties consumed by .ambient-bg's transform.
  private reducedMotion = false;
  private rafPending = false;

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private renderer: Renderer2
  ) {}

  ngOnInit() {
    this.reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.particles = this.generateParticles(10);
  }

  private generateParticles(count: number): Particle[] {
    const particles: Particle[] = [];
    for (let i = 0; i < count; i++) {
      particles.push({
        left: Math.random() * 100,
        top: Math.random() * 100,
        size: 2 + Math.random() * 2,
        duration: 18 + Math.random() * 14,
        delay: Math.random() * -20,
      });
    }
    return particles;
  }

  onMouseMove(event: MouseEvent) {
    if (this.reducedMotion || this.rafPending || !this.pageWrapperRef) return;
    const el = this.pageWrapperRef.nativeElement;
    const rect = el.getBoundingClientRect();
    const mx = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
    const my = ((event.clientY - rect.top) / rect.height - 0.5) * 2;

    this.rafPending = true;
    requestAnimationFrame(() => {
      this.renderer.setStyle(el, '--mx', mx.toFixed(3));
      this.renderer.setStyle(el, '--my', my.toFixed(3));
      this.rafPending = false;
    });
  }

  onMouseLeave() {
    if (!this.pageWrapperRef) return;
    const el = this.pageWrapperRef.nativeElement;
    this.renderer.setStyle(el, '--mx', '0');
    this.renderer.setStyle(el, '--my', '0');
  }

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
