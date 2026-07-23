import { Routes } from '@angular/router';
import { LoginComponent } from './login/login.component';
import { RegisterComponent } from './register/register.component';
import { FinanceLayoutComponent } from './finance/finance-layout/finance-layout.component';
import { FinanceHomeComponent } from './finance/home/finance-home.component';
import { FinanceUploadComponent } from './finance/upload/finance-upload.component';
import { FinanceOcrReviewComponent } from './finance/ocr-review/finance-ocr-review.component';
import { FinanceCorrectionsComponent } from './finance/corrections/finance-corrections.component';
import { FinanceCorrectionDetailComponent } from './finance/correction-detail/finance-correction-detail.component';
import { FinanceReportComponent } from './finance/report/finance-report.component';
import { FinanceTransactionsComponent } from './finance/transactions/finance-transactions.component';
import { FinanceTransactionCreateComponent } from './finance/transaction-create/finance-transaction-create.component';
import { FinanceTransactionDetailComponent } from './finance/transaction-detail/finance-transaction-detail.component';
import { AuditorLayoutComponent } from './auditor/auditor-layout/auditor-layout.component';
import { AuditorDashboardComponent } from './auditor/dashboard/auditor-dashboard.component';
import { AuditorReviewQueueComponent } from './auditor/review-queue/auditor-review-queue.component';
import { AuditorReportComponent } from './auditor/report/auditor-report.component';
import { AuditorRecordDetailComponent } from './auditor/record-detail/auditor-record-detail.component';
import { AuditorExceptionsComponent } from './auditor/exceptions/auditor-exceptions.component';
import { AuditorAnomaliesComponent } from './auditor/anomalies/auditor-anomalies.component';
import { AuditorAuthenticityComponent } from './auditor/authenticity/auditor-authenticity.component';
import { AuditorAuthenticityDetailComponent } from './auditor/authenticity-detail/auditor-authenticity-detail.component';
import { CalendarComponent } from './calendar/calendar.component';
import { AdminLayoutComponent } from './admin/admin-layout/admin-layout.component';
import { AdminDashboardComponent } from './admin/dashboard/admin-dashboard.component';
import { AdminUsersComponent } from './admin/users/admin-users.component';
import { AdminDocumentsComponent } from './admin/documents/admin-documents.component';
export const routes: Routes = [
  { path: '', redirectTo: 'login', pathMatch: 'full' },
  { path: 'login', component: LoginComponent },
  { path: 'register', component: RegisterComponent },
  {
    path: 'finance',
    component: FinanceLayoutComponent,
    children: [
      { path: 'home', component: FinanceHomeComponent },
      { path: 'upload', component: FinanceUploadComponent },
      { path: 'ocr-review', component: FinanceOcrReviewComponent },
      { path: 'corrections', component: FinanceCorrectionsComponent },
      { path: 'corrections/detail', component: FinanceCorrectionDetailComponent },
      { path: 'report', component: FinanceReportComponent },
      { path: 'calendar', component: CalendarComponent },
      { path: 'transactions', component: FinanceTransactionsComponent },
      { path: 'transactions/create', component: FinanceTransactionCreateComponent },
      { path: 'transactions/detail', component: FinanceTransactionDetailComponent },
      { path: '', redirectTo: 'home', pathMatch: 'full' }
    ]
  },
  {
    path: 'auditor',
    component: AuditorLayoutComponent,
    children: [
      { path: 'home', component: AuditorDashboardComponent },
      { path: 'review-queue', component: AuditorReviewQueueComponent },
      { path: 'record-detail', component: AuditorRecordDetailComponent },  // ← 新加
      { path: 'exceptions', component: AuditorExceptionsComponent },     // ← 新加
      { path: 'anomalies', component: AuditorAnomaliesComponent },
      { path: 'authenticity', component: AuditorAuthenticityComponent },
      { path: 'authenticity/:documentId', component: AuditorAuthenticityDetailComponent },
      { path: 'calendar', component: CalendarComponent },
      { path: 'report', component: AuditorReportComponent },
      { path: '', redirectTo: 'home', pathMatch: 'full' }
    ]
  },
  {
    path: 'admin',
    component: AdminLayoutComponent,
    children: [
      { path: 'home', component: AdminDashboardComponent },
      { path: 'users', component: AdminUsersComponent },
      { path: 'documents', component: AdminDocumentsComponent },
      { path: 'record-detail', component: AuditorRecordDetailComponent },
      { path: '', redirectTo: 'home', pathMatch: 'full' }
    ]
  },
  { path: '**', redirectTo: 'login' }
];