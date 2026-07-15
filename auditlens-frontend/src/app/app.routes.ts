import { Routes } from '@angular/router';
import { LoginComponent } from './login/login.component';
import { RegisterComponent } from './register/register.component';
import { RegisterSuccessComponent } from './register-success/register-success.component';
import { FinanceLayoutComponent } from './finance/finance-layout/finance-layout.component';
import { FinanceHomeComponent } from './finance/home/finance-home.component';
import { FinanceUploadComponent } from './finance/upload/finance-upload.component';
import { FinanceOcrReviewComponent } from './finance/ocr-review/finance-ocr-review.component';
import { FinanceReportComponent } from './finance/report/finance-report.component';
import { AuditorLayoutComponent } from './auditor/auditor-layout/auditor-layout.component';
import { AuditorDashboardComponent } from './auditor/dashboard/auditor-dashboard.component';
import { AuditorReportComponent } from './auditor/report/auditor-report.component';
import { AuditorRecordDetailComponent } from './auditor/record-detail/auditor-record-detail.component';
import { AuditorExceptionsComponent } from './auditor/exceptions/auditor-exceptions.component';
import { AuditorAnomaliesComponent } from './auditor/anomalies/auditor-anomalies.component';
import { AuditorAuthenticityComponent } from './auditor/authenticity/auditor-authenticity.component';
import { AuditorAuthenticityDetailComponent } from './auditor/authenticity-detail/auditor-authenticity-detail.component';
export const routes: Routes = [
  { path: '', redirectTo: 'login', pathMatch: 'full' },
  { path: 'login', component: LoginComponent },
  { path: 'register', component: RegisterComponent },
  { path: 'register-success', component: RegisterSuccessComponent },
  {
    path: 'finance',
    component: FinanceLayoutComponent,
    children: [
      { path: 'home', component: FinanceHomeComponent },
      { path: 'upload', component: FinanceUploadComponent },
      { path: 'ocr-review', component: FinanceOcrReviewComponent },
      { path: 'report', component: FinanceReportComponent },
      { path: '', redirectTo: 'home', pathMatch: 'full' }
    ]
  },
  {
    path: 'auditor',
    component: AuditorLayoutComponent,
    children: [
      { path: 'home', component: AuditorDashboardComponent },
      { path: 'record-detail', component: AuditorRecordDetailComponent },  // ← 新加
      { path: 'exceptions', component: AuditorExceptionsComponent },     // ← 新加
      { path: 'anomalies', component: AuditorAnomaliesComponent },
      { path: 'authenticity', component: AuditorAuthenticityComponent },
      { path: 'authenticity/:documentId', component: AuditorAuthenticityDetailComponent },
      { path: 'report', component: AuditorReportComponent },
      { path: '', redirectTo: 'home', pathMatch: 'full' }
    ]
  },
  { path: '**', redirectTo: 'login' }
];