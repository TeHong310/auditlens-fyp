import { Component, OnInit } from '@angular/core';
import { Router } from '@angular/router';

// Bare /user-manual entry point (e.g. a bookmarked or typed-in URL).
// The real, sidebar-integrated page lives at /finance/user-manual or
// /auditor/user-manual (same nesting convention every other page in
// this app already uses) — this just forwards there based on the
// logged-in user's own role, read the same way every layout component
// already reads it. Renders nothing itself.
@Component({
  selector: 'app-user-manual-redirect',
  standalone: true,
  template: ''
})
export class UserManualRedirectComponent implements OnInit {
  constructor(private router: Router) {}

  ngOnInit() {
    let role = '';
    if (typeof window !== 'undefined') {
      const user = JSON.parse(localStorage.getItem('user') || '{}');
      role = user?.role || '';
    }
    this.router.navigate([role === 'finance_executive' ? '/finance/user-manual' : '/auditor/user-manual']);
  }
}
