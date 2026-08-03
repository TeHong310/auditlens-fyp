import { Component, OnInit, ChangeDetectorRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, ActivatedRoute } from '@angular/router';
import { HttpClient, HttpHeaders } from '@angular/common/http';
import { environment } from '../../../environments/environment';
import { formatMalaysiaDateTime } from '../../shared/datetime.util';

// ── Send-Back structured form (Feature 1) — machine keys mirror
// helpers/send_back.py's REASON_CATEGORIES / REQUIRED_ACTIONS / PRIORITIES
// exactly, so the payload sent to POST /reviews/return/<id> validates
// cleanly server-side. Labels are the only thing translated to English
// here; the backend never sees the label text. ──
export type ReasonCategory =
  | 'missing_document' | 'incorrect_extracted_information' | 'invoice_po_gr_mismatch'
  | 'possible_duplicate_invoice' | 'authenticity_evidence_requires_clarification'
  | 'incorrect_supplier_information' | 'amount_or_quantity_requires_verification' | 'other';

export type RequiredAction =
  | 'upload_missing_document' | 'correct_extracted_information' | 'provide_written_explanation'
  | 'confirm_duplicate_submission' | 'replace_incorrect_document' | 'verify_amount_or_quantity'
  | 'confirm_supplier_information' | 'other';

export type Priority = 'normal' | 'medium' | 'high';

export interface SendBackFormState {
  reasonCategory: ReasonCategory | '';
  reasonOtherNote: string;
  instruction: string;
  requiredActions: RequiredAction[];
  requiredActionOtherNote: string;
  priority: Priority;
  dueDate: string;
}

const REASON_CATEGORY_OPTIONS: { key: ReasonCategory; label: string }[] = [
  { key: 'missing_document', label: 'Missing document' },
  { key: 'incorrect_extracted_information', label: 'Incorrect extracted information' },
  { key: 'invoice_po_gr_mismatch', label: 'Invoice / PO / GR mismatch' },
  { key: 'possible_duplicate_invoice', label: 'Possible duplicate invoice' },
  { key: 'authenticity_evidence_requires_clarification', label: 'Authenticity evidence requires clarification' },
  { key: 'incorrect_supplier_information', label: 'Incorrect supplier information' },
  { key: 'amount_or_quantity_requires_verification', label: 'Amount or quantity requires verification' },
  { key: 'other', label: 'Other' },
];

const REQUIRED_ACTION_OPTIONS: { key: RequiredAction; label: string }[] = [
  { key: 'upload_missing_document', label: 'Upload missing document' },
  { key: 'correct_extracted_information', label: 'Correct extracted information' },
  { key: 'provide_written_explanation', label: 'Provide written explanation' },
  { key: 'confirm_duplicate_submission', label: 'Confirm duplicate submission' },
  { key: 'replace_incorrect_document', label: 'Replace incorrect document' },
  { key: 'verify_amount_or_quantity', label: 'Verify amount or quantity' },
  { key: 'confirm_supplier_information', label: 'Confirm supplier information' },
  { key: 'other', label: 'Other' },
];

export function emptySendBackForm(): SendBackFormState {
  return {
    reasonCategory: '', reasonOtherNote: '', instruction: '',
    requiredActions: [], requiredActionOtherNote: '', priority: 'normal', dueDate: '',
  };
}

// Client-side mirror of helpers/send_back.py::validate_send_back_payload —
// instant feedback before the network round-trip; the backend re-
// validates the same rules and remains authoritative. Exported as a pure
// function (no DOM/HttpClient) so it's directly unit-testable.
export function validateSendBackForm(form: SendBackFormState, todayIso: string): string[] {
  const errors: string[] = [];
  if (!form.reasonCategory) errors.push('Please select a return reason category.');
  if (form.reasonCategory === 'other' && !form.reasonOtherNote.trim()) {
    errors.push('Please describe the "Other" reason.');
  }
  if (!form.instruction.trim()) errors.push('Auditor instruction is required.');
  if (form.requiredActions.length === 0) errors.push('Select at least one required action.');
  if (form.requiredActions.includes('other') && !form.requiredActionOtherNote.trim()) {
    errors.push('Please describe the "Other" required action.');
  }
  if (form.dueDate && form.dueDate < todayIso) {
    errors.push('Due date cannot be earlier than today.');
  }
  if (form.priority === 'high' && !form.dueDate) {
    errors.push('A response due date is required for high-priority send-back requests.');
  }
  return errors;
}

// Guided review checklist (Section 4/5) — the order Three-Way Matching
// -> Exception Review -> Authenticity Review -> Anomaly Review must be
// marked reviewed in, mirroring routes/reviews.py::REVIEW_STEP_ORDER
// exactly (server enforces the same order independently; this is only
// for the client-side unlock display, never the sole guard).
export const REVIEW_STEP_ORDER = ['three_way_matching', 'exception_review', 'authenticity_review', 'anomaly_review'] as const;
export type ReviewStep = typeof REVIEW_STEP_ORDER[number];

@Component({
  selector: 'app-auditor-record-detail',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './auditor-record-detail.component.html',
  styleUrls: ['./auditor-record-detail.component.css']
})
export class AuditorRecordDetailComponent implements OnInit {

  documentId: number | null = null;
  comparison: any = null;

  // Enterprise V3 Phase 6 — populated only when comparison.
  // transaction_context is present (this invoice belongs to a Finance
  // Transaction Package). null for a standalone/legacy invoice — every
  // *ngIf guarding the new sections below simply doesn't render, so
  // the page looks and behaves exactly as before Phase 6 for those.
  transactionDetail: any = null;
  authenticity: any = null;

  // Risk Indicators (below Exception Summary) — anomaly findings
  // (weekend transaction, amount anomaly, duplicate invoice, round
  // amount, etc.) for THIS document only, from the SAME anomalies data
  // the Document Workflow Timeline's "Anomaly Evaluation" step already
  // uses to decide blocking vs non-blocking (see loadRiskIndicators()
  // below) — no new endpoint, no mock data.
  riskIndicators: any[] = [];
  isLoading: boolean = false;
  isSubmitting: boolean = false;
  successMessage: string = '';
  errorMessage: string = '';
  auditNote: string = '';

  // Role of the currently logged-in user (read from localStorage, same
  // pattern used across every other layout/dashboard component). This
  // page is reached both from /auditor/record-detail (role always
  // 'auditor') and /admin/record-detail (role always 'admin') — the
  // Auditor's own Action Buttons above only render for 'auditor', and
  // are otherwise completely unaffected by this.
  userRole: string = '';

  // ── Admin Control (Admin module) — separate from the Auditor's own
  // approve/return above. Calls the admin-only /admin/documents/<id>/
  // approve and /admin/documents/<id>/send-back routes, never the
  // Auditor-gated /reviews/* routes. ──
  isAdminSubmitting: boolean = false;
  adminActionSuccess: string = '';
  adminActionError: string = '';
  showAdminSendBackModal: boolean = false;
  adminSendBackTarget: 'finance' | 'auditor' = 'finance';
  adminReason: string = '';
  adminMessage: string = '';
  adminSendBackError: string = '';

  // ── Send-Back workflow (Features 1, 4, 5) ──
  reasonCategoryOptions = REASON_CATEGORY_OPTIONS;
  requiredActionOptions = REQUIRED_ACTION_OPTIONS;
  showSendBackModal: boolean = false;
  sendBack: SendBackFormState = emptySendBackForm();
  sendBackErrors: string[] = [];
  cycles: any[] = [];
  reviewHistory: any[] = [];

  // ── AI Audit Assistant — contextual help for THIS case only, called
  // ONLY when the auditor clicks a button (see ngOnInit: no AI call is
  // ever triggered on page load). Backed by POST /ai-assistant/<id>/*. ──
  aiActionLoading: { [key: string]: boolean } = {};
  aiError: string = '';
  aiCaseSummary: { audit_status: string; reason: string; recommended_action: string } | null = null;
  aiRisk: { risk_level: string; reasons: string[]; potential_impact: string } | null = null;
  // approval_readiness is server-computed deterministically (never the
  // AI's own guess — see routes/ai_assistant.py::_clamp_approval_
  // assessment_result), same guarantee aiCaseSummary's audit_status
  // already has — this label can never contradict the actual matching/
  // authenticity/anomaly state, and never makes the actual approval
  // decision (the auditor still does that via the Approve/Send Back/
  // Need Review buttons below, unchanged).
  aiApprovalAssessment: {
    approval_readiness: string; blocking_issues: string[]; passed_checks: string[];
    risk_context: string[]; recommended_next_steps: string[];
  } | null = null;
  aiQuestion: string = '';
  aiConversation: { question: string; answer: string }[] = [];

  // ── Guided review checklist (Section 4/5) — one entry per step once
  // marked reviewed (null/absent until then), loaded alongside
  // riskIndicators from the SAME GET /documents/<id>/timeline call
  // (see loadRiskIndicators() below) — no extra request. ──
  reviewSteps: { [key: string]: { reviewed_by: number; reviewer_name: string; reviewed_at: string } } = {};
  markingStep: ReviewStep | null = null;
  markStepError: string = '';

  private apiUrl = environment.apiUrl;

  constructor(
    private http: HttpClient,
    private router: Router,
    private route: ActivatedRoute,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit() {
    if (typeof window !== 'undefined') {
      const u = JSON.parse(localStorage.getItem('user') || '{}');
      this.userRole = u?.role || '';
    }
    this.route.queryParams.subscribe(params => {
      if (params['document_id']) {
        this.documentId = parseInt(params['document_id']);
        this.loadComparison();
        this.loadAuthenticity();
        this.loadCycles();
        this.loadReviewHistory();
        this.loadRiskIndicators();
      }
    });
  }

  getHeaders() {
    const token = localStorage.getItem('access_token');
    return new HttpHeaders({ 'Authorization': `Bearer ${token}` });
  }

  // ── Load comparison ─────────────────────────────────────

  loadComparison() {
    if (!this.documentId) return;
    this.isLoading = true;
    this.errorMessage = '';
    this.http.get<any>(`${this.apiUrl}/auditor/record/${this.documentId}/comparison`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.comparison = res;
        this.isLoading = false;
        this.cdr.detectChanges();
        if (res.transaction_context) {
          this.loadTransactionDetail(res.transaction_context.transaction_package_id);
        }
      },
      error: (err) => {
        this.isLoading = false;
        this.errorMessage = err.error?.error || 'Failed to load record comparison.';
        this.cdr.detectChanges();
      }
    });
  }

  // Enterprise V3 Phase 6 (STEP 4/5/6) — the richer transaction-level
  // view (documents by role, Enterprise Matching Summary, authenticity
  // summary, relationship preview). Only ever called when comparison.
  // transaction_context said this invoice belongs to a package; a
  // failure here doesn't block the rest of the page (the existing
  // Field Comparison / Audit Decision sections already loaded fine).
  loadTransactionDetail(packageId: number) {
    this.http.get<any>(`${this.apiUrl}/auditor/transactions/${packageId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.transactionDetail = res;
        this.cdr.detectChanges();
      },
      error: () => { /* non-blocking — transaction sections simply stay hidden */ }
    });
  }

  // ── Authenticity warning banner ─────────────────────────
  // Advisory only (Layer 6 soft gate) — informational, never blocks
  // the review flow below. A 404 (no check run / not yet detected)
  // is expected and silent, not an error.

  loadAuthenticity() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/authenticity/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.authenticity = res;
        this.cdr.detectChanges();
      },
      error: () => {
        this.authenticity = null;
      }
    });
  }

  get showAuthenticityWarning(): boolean {
    return this.authenticity?.authenticity_status === 'warning';
  }

  authenticityWarningReason(): string {
    if (!this.authenticity) return '';
    const missing: string[] = [];
    if (!this.authenticity.has_company_name) missing.push('company name');
    if (!this.authenticity.has_company_chop && !this.authenticity.has_signature) {
      missing.push('signature and company chop');
    }
    const docLabel = this.authenticity.document_type === 'invoice' ? 'Invoice'
      : this.authenticity.document_type === 'po' ? 'PO' : 'GR';
    if (missing.length === 0) return `Authenticity signals below expected threshold on ${docLabel}.`;
    return `Missing ${missing.join(' and ')} on ${docLabel}.`;
  }

  authenticitySourceIcon(): string {
    const source = this.authenticity?.upload_source;
    if (source === 'phone_photo') return 'ph-device-mobile-camera';
    if (source === 'scanned') return 'ph-printer';
    if (source === 'digital_native') return 'ph-desktop';
    if (source === 'webcam') return 'ph-webcam';
    return 'ph-question';
  }

  authenticitySourceLabel(): string {
    const source = this.authenticity?.upload_source;
    if (source === 'phone_photo') return 'Phone Photo';
    if (source === 'scanned') return 'Scanned';
    if (source === 'digital_native') return 'Digital Native';
    if (source === 'webcam') return 'Webcam';
    return 'Unknown';
  }

  // ── Risk Indicators (below Exception Summary) ───────────
  // Reuses GET /documents/<id>/timeline for BOTH the anomaly findings
  // (Risk Indicators) AND the guided review checklist's progress
  // (reviewSteps, Section 4/5) — one call, no separate endpoint. Non-
  // blocking: a failure here just leaves both sections showing nothing
  // marked/reviewed, matching every other advisory section on this page
  // (authenticity, transaction detail).
  loadRiskIndicators() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/documents/${this.documentId}/timeline`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => {
        this.riskIndicators = res.anomalies || [];
        this.reviewSteps = res.review_steps || {};
        this.cdr.detectChanges();
      },
      error: () => { this.riskIndicators = []; this.reviewSteps = {}; }
    });
  }

  // ── Guided review checklist (Section 4/5) ───────────────
  // Three-Way Matching -> Exception Review -> Authenticity Review ->
  // Anomaly Review, each gated on the one before it. "Mark as
  // Reviewed" never navigates — it only saves reviewer/time and
  // unlocks the next step; Send Back and Need Review stay available
  // regardless of how many steps are done (only Approve is gated,
  // see canApprove/approveDisabledReason below).

  isStepReviewed(step: ReviewStep): boolean {
    return !!this.reviewSteps[step];
  }

  isStepUnlocked(step: ReviewStep): boolean {
    const idx = REVIEW_STEP_ORDER.indexOf(step);
    if (idx <= 0) return true;
    return REVIEW_STEP_ORDER.slice(0, idx).every(s => this.isStepReviewed(s));
  }

  // The step this page should point the auditor at next — used only to
  // name the prerequisite in the "locked" note under a not-yet-
  // unlocked step (e.g. "Complete Three-Way Matching first").
  stepLabel(step: ReviewStep): string {
    if (step === 'three_way_matching') return 'Three-Way Matching';
    if (step === 'exception_review') return 'Exception Review';
    if (step === 'authenticity_review') return 'Authenticity Review';
    return 'Anomaly Review';
  }

  firstLockedPrerequisite(step: ReviewStep): string {
    const idx = REVIEW_STEP_ORDER.indexOf(step);
    const missing = REVIEW_STEP_ORDER.slice(0, idx).find(s => !this.isStepReviewed(s));
    return missing ? this.stepLabel(missing) : '';
  }

  markStepReviewed(step: ReviewStep) {
    if (!this.documentId || this.markingStep || !this.isStepUnlocked(step)) return;
    this.markingStep = step;
    this.markStepError = '';
    this.http.post<any>(`${this.apiUrl}/reviews/review-steps/${this.documentId}/${step}`, {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.markingStep = null;
        this.reviewSteps = {
          ...this.reviewSteps,
          [step]: { reviewed_by: res.reviewed_by, reviewer_name: res.reviewer_name, reviewed_at: res.reviewed_at }
        };
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.markingStep = null;
        this.markStepError = err.error?.error || 'Failed to mark this step as reviewed.';
        this.cdr.detectChanges();
      }
    });
  }

  // System result shown under each step — reuses data this page already
  // loads for Case Summary/Exception Summary/Authenticity/Risk
  // Indicators, so the checklist can never disagree with the rest of
  // the page about what it found.

  get matchingStepResult(): string {
    return `Status: ${this.overallStatus}`;
  }

  get exceptionStepResult(): string {
    return this.exceptionIssueTitle ? `${this.exceptionIssueTitle} (${this.exceptionRiskLevel} risk)` : 'No exceptions detected';
  }

  get authenticityStepResult(): string {
    if (!this.authenticity) return 'Not yet checked';
    return this.authenticity.authenticity_status === 'passed' ? 'Status: Passed' : 'Status: Warning';
  }

  get anomalyStepResult(): string {
    if (this.caseBlockingIssues !== 'None') return `Blocking: ${this.caseBlockingIssues}`;
    if (this.openFindings.length) return `No blocking issues — ${this.openFindingsSummary} on record`;
    return 'No anomalies detected';
  }

  // Only Approve is gated — Send Back and Need Review remain available
  // at every stage of the checklist (Section 6).
  get allMandatoryStepsReviewed(): boolean {
    return REVIEW_STEP_ORDER.every(s => this.isStepReviewed(s));
  }

  get canApprove(): boolean {
    return this.allMandatoryStepsReviewed && this.caseBlockingIssues === 'None';
  }

  get approveDisabledReason(): string {
    if (this.caseBlockingIssues !== 'None') return `Blocking issue on record: ${this.caseBlockingIssues}.`;
    if (!this.allMandatoryStepsReviewed) {
      const nextStep = REVIEW_STEP_ORDER.find(s => !this.isStepReviewed(s));
      return nextStep ? `Complete "${this.stepLabel(nextStep)}" before approving.` : 'Complete all review steps before approving.';
    }
    return '';
  }

  // ── "View Details" links — each opens the relevant case-specific
  // page with this record's document_id (and transaction_package_id
  // when this invoice belongs to one), so every one of those pages can
  // show a "Back to Audit Review" button that returns to THIS exact
  // record, never a global list. ──

  private get transactionPackageId(): number | null {
    return this.comparison?.transaction_context?.transaction_package_id ?? null;
  }

  openMatchingDetails() {
    const queryParams: any = { document_id: this.documentId };
    if (this.transactionPackageId) queryParams.transaction_package_id = this.transactionPackageId;
    this.router.navigate(['/auditor/matching-details'], { queryParams });
  }

  openExceptionDetails() {
    this.router.navigate(['/auditor/exceptions'], { queryParams: { document_id: this.documentId, ref: 'audit-review' } });
  }

  openAuthenticityDetails() {
    const queryParams: any = { document_id: this.documentId, ref: 'audit-review' };
    if (this.comparison?.invoice?.invoice_no) queryParams.invoice_no = this.comparison.invoice.invoice_no;
    this.router.navigate(['/auditor/authenticity'], { queryParams });
  }

  openAnomalyDetails() {
    this.router.navigate(['/auditor/anomalies'], { queryParams: { document_id: this.documentId, ref: 'audit-review' } });
  }

  // Icon/label/severity mapping deliberately mirrors auditor-anomalies.
  // component.ts (the full Anomaly Detection page) exactly, so the same
  // finding reads identically in both places — but this section only
  // ever shows type + severity + review status, never the Evidence
  // Found / AI Assessment / Suggested Checks detail or the Investigate/
  // Review/Dismiss actions that make that page the "full" one.
  riskTypeIcon(type: string): string {
    if (type === 'amount') return 'ph-currency-circle-dollar';
    if (type === 'round') return 'ph-target';
    if (type === 'weekend') return 'ph-calendar-blank';
    if (type === 'duplicate') return 'ph-repeat';
    return 'ph-question';
  }

  riskTypeLabel(type: string): string {
    if (type === 'amount') return 'Amount Anomaly';
    if (type === 'round') return 'Round Amount';
    if (type === 'weekend') return 'Weekend Transaction';
    if (type === 'duplicate') return 'Possible Duplicate Invoice';
    return 'Anomaly';
  }

  riskSeverityClass(severity: string): string {
    if (severity === 'high') return 'pill-high';
    if (severity === 'medium') return 'pill-medium';
    return 'pill-low';
  }

  // Canonical status wording — matches the actual status VALUE, same
  // rename as auditor-anomalies.component.ts's statusLabel() so an
  // anomaly reads identically on both pages. 'Reviewed' never implies
  // "cleared" — only 'Dismissed' does.
  riskStatusLabel(status: string): string {
    if (status === 'reviewed') return 'Reviewed';
    if (status === 'dismissed') return 'Dismissed';
    return 'Pending';
  }

  // ── Case Summary (Matching Status / Audit Decision / Overall Risk /
  // Open Findings / Blocking Issues) — a compact, ALWAYS-available
  // snapshot built purely from data this page already loads on open
  // (comparison, riskIndicators, reviewHistory), no AI call required.
  // Matching (the matching engine's own PASS/REVIEW/PARTIAL verdict)
  // and Audit Decision (what the Auditor actually decided, or hasn't
  // yet) are DELIBERATELY two separate facts here — a PASS match never
  // implies "Ready for approval" on its own; see getBannerText/
  // Subtitle below, which apply the SAME Send Back > Need Review >
  // material-finding priority to the page's main status banner. ──

  // Single source of truth for "what did the Auditor last decide" —
  // reviewHistory is ASC-ordered (GET /reviews/history), so the last
  // element is the most recent action; null before any decision.
  private get latestReviewAction(): string | null {
    return this.reviewHistory.length ? this.reviewHistory[this.reviewHistory.length - 1].action : null;
  }

  get caseBlockingIssues(): string {
    const blocking = this.riskIndicators.filter(a => a.classification === 'blocking');
    if (!blocking.length) return 'None';
    return blocking.map(a => `${this.riskTypeLabel(a.anomaly_type)} (${a.severity} severity)`).join(', ');
  }

  get caseFinalDecision(): string {
    const action = this.latestReviewAction;
    if (action === 'approved') return 'Approved';
    if (action === 'returned') return 'Sent Back to Finance';
    if (action === 'need_review') return 'Need Review';
    return 'Awaiting Auditor';
  }

  // Final Audit Decision timeline step marker — approved reads as
  // completed (green), a Send Back/Need Review decision reads as
  // action_required (amber, same "needs attention" language the
  // Three-Way Matching/Exception/Authenticity/Anomaly steps already
  // use), and no decision yet reads as pending (grey).
  get finalDecisionStepClass(): string {
    const action = this.latestReviewAction;
    if (action === 'approved') return 'wt-completed';
    if (action === 'returned' || action === 'need_review') return 'wt-action-required';
    return 'wt-pending';
  }

  get finalDecisionStepIcon(): string {
    const action = this.latestReviewAction;
    if (action === 'approved') return 'ph-check-circle';
    if (action === 'returned' || action === 'need_review') return 'ph-warning';
    return 'ph-circle';
  }

  auditDecisionChipClass(): string {
    const action = this.latestReviewAction;
    if (action === 'returned') return 'risk-high';
    if (action === 'need_review') return 'risk-medium';
    return 'risk-low'; // approved, or no decision yet — neither is itself a risk signal
  }

  // "Open" = every anomaly still on record that wasn't dismissed as a
  // false positive (pending OR reviewed) — reviewed never means
  // cleared, so it stays counted as an open finding here too.
  get openFindings(): any[] {
    return this.riskIndicators.filter(a => a.status !== 'dismissed');
  }

  get openFindingsSummary(): string {
    if (!this.openFindings.length) return 'None';
    if (this.openFindings.length === 1) {
      const a = this.openFindings[0];
      return `1 ${this.riskStatusLabel(a.status)} ${this.riskTypeLabel(a.anomaly_type)}`;
    }
    return this.openFindings.map(a => `${this.riskStatusLabel(a.status)} ${this.riskTypeLabel(a.anomaly_type)}`).join(', ');
  }

  get overallRiskLevel(): string {
    if (this.openFindings.some(a => a.severity === 'high')) return 'HIGH';
    if (this.openFindings.some(a => a.severity === 'medium')) return 'MEDIUM';
    if (this.openFindings.length) return 'LOW';
    return 'NONE';
  }

  overallRiskChipClass(): string {
    if (this.overallRiskLevel === 'HIGH') return 'risk-high';
    if (this.overallRiskLevel === 'MEDIUM') return 'risk-medium';
    return 'risk-low'; // LOW or NONE
  }

  // "Material" = pending AND medium/high severity — the bar for the
  // amber "Risk Review Required" banner state and the prominent risk
  // panel below. Deliberately NOT every anomaly (a low-severity or
  // already reviewed/dismissed finding never trips this) — the task
  // this implements explicitly rules out auto-classifying every
  // anomaly as blocking/urgent.
  get materialFindings(): any[] {
    return this.riskIndicators.filter(a => a.status === 'pending' && (a.severity === 'medium' || a.severity === 'high'));
  }

  get hasMaterialFinding(): boolean {
    return this.materialFindings.length > 0;
  }

  get materialFindingsPanelText(): string {
    if (!this.materialFindings.length) return '';
    if (this.materialFindings.length === 1) {
      const a = this.materialFindings[0];
      const severityWord = a.severity === 'high' ? 'High' : 'Medium';
      return `1 pending ${severityWord} ${this.riskTypeLabel(a.anomaly_type)} anomaly requires Auditor follow-up.`;
    }
    return `${this.materialFindings.length} pending Medium/High anomalies require Auditor follow-up.`;
  }

  // ── Send-Back cycles + review history (Features 4, 5) ───
  // Two separate, deliberately UN-merged data sources:
  //   - cycles: the structured send-back detail (reason/instruction/
  //     required actions/priority/due date/Finance response) — powers
  //     the "Finance Response" + "Changes Since Send Back" panels.
  //   - reviewHistory: review_records, the EXISTING audit-log system
  //     (see helpers/audit_log.py / routes/reviews.py) — already has
  //     everything the History timeline needs (action, remarks,
  //     reviewer, timestamp), so it's used as-is rather than building a
  //     second competing log.

  loadCycles() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/reviews/send-back-cycles/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => { this.cycles = res.cycles || []; this.cdr.detectChanges(); },
      error: () => { this.cycles = []; }
    });
  }

  loadReviewHistory() {
    if (!this.documentId) return;
    this.http.get<any>(`${this.apiUrl}/reviews/history/${this.documentId}`, {
      headers: this.getHeaders()
    }).subscribe({
      next: (res) => { this.reviewHistory = res.history || []; this.cdr.detectChanges(); },
      error: () => { this.reviewHistory = []; }
    });
  }

  get latestCycle(): any {
    return this.cycles.length ? this.cycles[this.cycles.length - 1] : null;
  }

  get hasFinanceResponse(): boolean {
    return !!this.latestCycle?.finance_response;
  }

  get changesSinceSendBack(): string[] {
    return this.latestCycle?.activity_summary || [];
  }

  reasonCategoryLabel(key: string): string {
    return REASON_CATEGORY_OPTIONS.find(o => o.key === key)?.label || key;
  }

  requiredActionLabel(key: string): string {
    return REQUIRED_ACTION_OPTIONS.find(o => o.key === key)?.label || key;
  }

  priorityLabel(p: string): string {
    if (p === 'high') return 'High';
    if (p === 'medium') return 'Medium';
    return 'Normal';
  }

  priorityClass(p: string): string {
    if (p === 'high') return 'priority-high';
    if (p === 'medium') return 'priority-medium';
    return 'priority-normal';
  }

  historyLabel(action: string): string {
    if (action === 'returned') return 'Record sent back to Finance';
    if (action === 'resubmitted') return 'Record resubmitted for auditor review';
    if (action === 'approved') return 'Record approved';
    if (action === 'need_review') return 'Marked for further review';
    if (action === 'closed') return 'Correction case closed';
    return action;
  }

  formatDateTime(dateStr: string): string {
    return formatMalaysiaDateTime(dateStr);
  }

  // ── Send-Back modal (Feature 1) ──────────────────────────

  get todayIso(): string {
    return new Date().toISOString().slice(0, 10);
  }

  get sendBackButtonLabel(): string {
    return this.cycles.length > 0 ? 'Send Back Again' : 'Send Back to Finance';
  }

  openSendBackModal() {
    this.sendBack = emptySendBackForm();
    // UI guidance only — pre-fills the obvious reason/required action
    // for a missing-document record so the auditor doesn't have to
    // re-derive what's already visible in the Field Comparison table.
    // Every field remains fully editable; send-back validation/
    // submission logic is untouched.
    if (this.isMissingDocumentsIssue) {
      this.sendBack.reasonCategory = 'missing_document';
      this.sendBack.requiredActions = ['upload_missing_document'];
    }
    this.sendBackErrors = [];
    this.showSendBackModal = true;
  }

  closeSendBackModal() {
    this.showSendBackModal = false;
  }

  toggleRequiredAction(key: RequiredAction) {
    const i = this.sendBack.requiredActions.indexOf(key);
    if (i === -1) this.sendBack.requiredActions.push(key);
    else this.sendBack.requiredActions.splice(i, 1);
  }

  isRequiredActionChecked(key: RequiredAction): boolean {
    return this.sendBack.requiredActions.includes(key);
  }

  submitSendBack() {
    if (!this.documentId || this.isSubmitting) return;

    const errors = validateSendBackForm(this.sendBack, this.todayIso);
    if (errors.length) {
      this.sendBackErrors = errors;
      this.cdr.detectChanges();
      return;
    }
    this.sendBackErrors = [];
    this.isSubmitting = true;

    const payload: any = {
      reason_category: this.sendBack.reasonCategory,
      instruction: this.sendBack.instruction.trim(),
      required_actions: this.sendBack.requiredActions,
      priority: this.sendBack.priority,
    };
    if (this.sendBack.reasonOtherNote.trim()) payload.reason_other_note = this.sendBack.reasonOtherNote.trim();
    if (this.sendBack.requiredActionOtherNote.trim()) {
      payload.required_action_other_note = this.sendBack.requiredActionOtherNote.trim();
    }
    if (this.sendBack.dueDate) payload.due_date = this.sendBack.dueDate;

    this.http.post<any>(`${this.apiUrl}/reviews/return/${this.documentId}`,
      payload,
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.showSendBackModal = false;
        this.successMessage = 'Document returned to Finance!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/auditor/home']);
        }, 2000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.sendBackErrors = [err.error?.error || 'Failed to send back.'];
        this.cdr.detectChanges();
      }
    });
  }

  // ── Audit decision actions ──────────────────────────────

  approveDocument() {
    if (!this.documentId || !this.canApprove) return;
    this.isSubmitting = true;
    this.http.post<any>(`${this.apiUrl}/reviews/approve/${this.documentId}`,
      { remarks: this.auditNote },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = 'Document approved successfully!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/auditor/home']);
        }, 2000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.errorMessage = err.error?.error || 'Failed to approve.';
        this.cdr.detectChanges();
      }
    });
  }

  // The third final-decision control (see routes/reviews.py's POST
  // /reviews/need-review/<id>). Unlike Approve/Send Back this is not a
  // workflow-ending disposition — the document and its anomalies stay
  // exactly as they are (any 'pending' anomaly stays pending, same as
  // Send Back — the issue is not resolved) — so this stays ON the page
  // and refreshes the review history instead of navigating away.
  needReviewDocument() {
    if (!this.documentId || this.isSubmitting) return;
    this.isSubmitting = true;
    this.http.post<any>(`${this.apiUrl}/reviews/need-review/${this.documentId}`,
      { remarks: this.auditNote },
      { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isSubmitting = false;
        this.successMessage = 'Marked as needing further review.';
        this.loadReviewHistory();
        this.cdr.detectChanges();
        setTimeout(() => { this.successMessage = ''; this.cdr.detectChanges(); }, 5000);
      },
      error: (err) => {
        this.isSubmitting = false;
        this.errorMessage = err.error?.error || 'Failed to mark as needing review.';
        this.cdr.detectChanges();
      }
    });
  }

  goBack() {
    if (this.userRole === 'admin') {
      this.router.navigate(['/admin/documents']);
    } else {
      this.router.navigate(['/auditor/home']);
    }
  }

  // ── Admin Control (Admin module) ──────────────────────────

  adminApproveDocument() {
    if (!this.documentId || this.isAdminSubmitting) return;
    this.isAdminSubmitting = true;
    this.adminActionError = '';
    this.adminActionSuccess = '';
    this.http.post<any>(`${this.apiUrl}/admin/documents/${this.documentId}/approve`,
      {}, { headers: this.getHeaders() }
    ).subscribe({
      next: () => {
        this.isAdminSubmitting = false;
        this.adminActionSuccess = 'Document approved successfully!';
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/admin/documents']);
        }, 2000);
      },
      error: (err) => {
        this.isAdminSubmitting = false;
        this.adminActionError = err.error?.error || 'Failed to approve.';
        this.cdr.detectChanges();
      }
    });
  }

  openAdminSendBackModal(target: 'finance' | 'auditor') {
    this.adminSendBackTarget = target;
    this.adminReason = '';
    this.adminMessage = '';
    this.adminSendBackError = '';
    this.showAdminSendBackModal = true;
  }

  closeAdminSendBackModal() {
    if (this.isAdminSubmitting) return;
    this.showAdminSendBackModal = false;
  }

  submitAdminSendBack() {
    if (!this.documentId || this.isAdminSubmitting) return;

    if (!this.adminReason.trim() || !this.adminMessage.trim()) {
      this.adminSendBackError = 'Reason and message are required.';
      return;
    }

    this.isAdminSubmitting = true;
    this.adminSendBackError = '';
    this.adminActionError = '';
    this.adminActionSuccess = '';

    this.http.post<any>(`${this.apiUrl}/admin/documents/${this.documentId}/send-back`, {
      target: this.adminSendBackTarget,
      reason: this.adminReason.trim(),
      message: this.adminMessage.trim(),
    }, { headers: this.getHeaders() }).subscribe({
      next: () => {
        this.isAdminSubmitting = false;
        this.showAdminSendBackModal = false;
        this.adminActionSuccess = `Document sent back to ${this.adminSendBackTarget === 'finance' ? 'Finance' : 'Auditor'}!`;
        this.cdr.detectChanges();
        setTimeout(() => {
          this.router.navigate(['/admin/documents']);
        }, 2000);
      },
      error: (err) => {
        this.isAdminSubmitting = false;
        this.adminSendBackError = err.error?.error || 'Failed to send back.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── AI Audit Assistant ───────────────────────────────────
  // Every method here is triggered ONLY by an explicit button click.
  // Each POST call is scoped to this one document_id and returns a
  // response derived only from data already computed by AuditLens'
  // matching/authenticity/anomaly engines (backend: routes/ai_
  // assistant.py). Final approval/send-back decisions remain fully
  // manual — these only draft text/pre-fill fields for the auditor to
  // review and edit.

  explainException() {
    if (!this.documentId) return;
    this.aiActionLoading['explain_exception'] = true;
    this.aiError = '';
    this.http.post<any>(`${this.apiUrl}/ai-assistant/${this.documentId}/explain-exception`, {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.aiActionLoading['explain_exception'] = false;
        // audit_status is server-computed deterministically (never the
        // AI's own guess — see routes/ai_assistant.py::_clamp_explain_
        // exception_result) so this label can never contradict the
        // actual matching/authenticity/anomaly state.
        this.aiCaseSummary = {
          audit_status: res.audit_status || 'REVIEW REQUIRED',
          reason: res.reason || '',
          recommended_action: res.recommended_action || ''
        };
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.aiActionLoading['explain_exception'] = false;
        this.aiError = err.error?.error || 'AI Assistant is unavailable right now.';
        this.cdr.detectChanges();
      }
    });
  }

  explainRisk() {
    if (!this.documentId) return;
    this.aiActionLoading['explain_risk'] = true;
    this.aiError = '';
    this.http.post<any>(`${this.apiUrl}/ai-assistant/${this.documentId}/explain-risk`, {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.aiActionLoading['explain_risk'] = false;
        this.aiRisk = {
          risk_level: res.risk_level || 'Low',
          reasons: res.reasons || [],
          potential_impact: res.potential_impact || ''
        };
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.aiActionLoading['explain_risk'] = false;
        this.aiError = err.error?.error || 'AI Assistant is unavailable right now.';
        this.cdr.detectChanges();
      }
    });
  }

  approvalAssessment() {
    if (!this.documentId) return;
    this.aiActionLoading['approval_assessment'] = true;
    this.aiError = '';
    this.http.post<any>(`${this.apiUrl}/ai-assistant/${this.documentId}/approval-assessment`, {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.aiActionLoading['approval_assessment'] = false;
        this.aiApprovalAssessment = {
          approval_readiness: res.approval_readiness || 'Requires Review',
          blocking_issues: res.blocking_issues || [],
          passed_checks: res.passed_checks || [],
          risk_context: res.risk_context || [],
          recommended_next_steps: res.recommended_next_steps || []
        };
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.aiActionLoading['approval_assessment'] = false;
        this.aiError = err.error?.error || 'AI Assistant is unavailable right now.';
        this.cdr.detectChanges();
      }
    });
  }

  // Reuses the SAME risk-chip color classes as exceptionRiskClass/
  // auditStatusClass above — Ready reads as "low risk" (green), Not
  // Ready as "high risk" (red), Requires Review as "needs attention"
  // (amber) — no new CSS needed.
  approvalReadinessClass(readiness: string): string {
    if (readiness === 'Ready') return 'risk-low';
    if (readiness === 'Not Ready') return 'risk-high';
    return 'risk-medium';
  }

  // Same icon set as getBannerIcon() above (PASS/FAIL/else -> check/x/
  // warning) so Approval Readiness reads with the same at-a-glance
  // visual language already established for the overall status banner.
  approvalReadinessIcon(readiness: string): string {
    if (readiness === 'Ready') return 'ph-check-circle';
    if (readiness === 'Not Ready') return 'ph-x-circle';
    return 'ph-warning';
  }

  generateAuditRemark() {
    if (!this.documentId) return;
    this.aiActionLoading['generate_remark'] = true;
    this.aiError = '';
    this.http.post<any>(`${this.apiUrl}/ai-assistant/${this.documentId}/generate-remark`, {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.aiActionLoading['generate_remark'] = false;
        // Populates the EXISTING Remarks/Notes textarea below — the
        // auditor can still edit or clear it before approving/sending
        // back; nothing here is auto-saved.
        this.auditNote = res.remark || '';
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.aiActionLoading['generate_remark'] = false;
        this.aiError = err.error?.error || 'AI Assistant is unavailable right now.';
        this.cdr.detectChanges();
      }
    });
  }

  prepareSendBackInstruction() {
    if (!this.documentId) return;
    this.aiActionLoading['prepare_send_back'] = true;
    this.aiError = '';
    this.http.post<any>(`${this.apiUrl}/ai-assistant/${this.documentId}/prepare-send-back`, {},
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.aiActionLoading['prepare_send_back'] = false;
        // Opens the EXISTING Send Back modal, pre-filled with the AI's
        // suggestion — every field stays fully editable and nothing is
        // sent until the auditor clicks "Send Back to Finance"
        // themselves (existing submitSendBack() flow, untouched).
        this.sendBack = {
          reasonCategory: res.reason_category,
          reasonOtherNote: '',
          instruction: res.instruction || '',
          requiredActions: res.required_actions || [],
          requiredActionOtherNote: '',
          priority: res.priority || 'normal',
          dueDate: '',
        };
        this.sendBackErrors = [];
        this.showSendBackModal = true;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.aiActionLoading['prepare_send_back'] = false;
        this.aiError = err.error?.error || 'AI Assistant is unavailable right now.';
        this.cdr.detectChanges();
      }
    });
  }

  askAiQuestion() {
    if (!this.documentId || !this.aiQuestion.trim() || this.aiActionLoading['ask']) return;
    const question = this.aiQuestion.trim();
    this.aiActionLoading['ask'] = true;
    this.aiError = '';
    this.http.post<any>(`${this.apiUrl}/ai-assistant/${this.documentId}/ask`, { question },
      { headers: this.getHeaders() }
    ).subscribe({
      next: (res) => {
        this.aiActionLoading['ask'] = false;
        this.aiConversation.push({ question, answer: res.answer || '' });
        this.aiQuestion = '';
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.aiActionLoading['ask'] = false;
        this.aiError = err.error?.error || 'AI Assistant is unavailable right now.';
        this.cdr.detectChanges();
      }
    });
  }

  // ── Overall status banner ───────────────────────────────

  get overallStatus(): string {
    return this.comparison?.match_result?.overall_status || 'PARTIAL';
  }

  // Priority cascade — Send Back > Need Review > a pending Medium/High
  // anomaly > the underlying matching result. Deliberately checked
  // BEFORE overallStatus in every method below: a PASS matching result
  // must never override an outstanding Auditor decision or an
  // unresolved material finding just because the fields themselves
  // matched. Matching (Case Summary's own "Matching" row/chip, and
  // matchingStatusChipClass() below) is intentionally untouched by any
  // of this — it always reflects the raw matching engine verdict.
  getBannerClass(): string {
    const action = this.latestReviewAction;
    if (action === 'returned') return 'banner-fail';
    if (action === 'need_review' || this.hasMaterialFinding) return 'banner-partial';
    if (this.overallStatus === 'PASS') return 'banner-pass';
    if (this.overallStatus === 'FAIL') return 'banner-fail';
    // REVIEW reuses the same amber styling as PARTIAL (banner-partial) —
    // both are "needs attention, not a hard failure" states; only the
    // text differs (see getBannerText/getBannerSubtitle).
    return 'banner-partial';
  }

  // Case Summary's compact Matching chip reuses the SAME .risk-chip
  // pill family Risk Indicators/AI Assistant already use elsewhere on
  // this page (risk-high/risk-medium/risk-low), not the full-banner
  // gradient classes above — a small pill needs a small-pill palette.
  matchingStatusChipClass(): string {
    if (this.overallStatus === 'PASS') return 'risk-low';
    if (this.overallStatus === 'FAIL') return 'risk-high';
    return 'risk-medium';
  }

  getBannerIcon(): string {
    const action = this.latestReviewAction;
    if (action === 'returned') return 'ph-x-circle';
    if (action === 'need_review' || this.hasMaterialFinding) return 'ph-warning';
    if (this.overallStatus === 'PASS') return 'ph-check-circle';
    if (this.overallStatus === 'FAIL') return 'ph-x-circle';
    return 'ph-warning';
  }

  getBannerText(): string {
    const action = this.latestReviewAction;
    if (action === 'returned') return 'Correction Required';
    if (action === 'need_review') return 'Further Review Required';
    if (this.hasMaterialFinding) return 'Risk Review Required';
    if (this.overallStatus === 'PASS') return 'All Fields Match';
    if (this.overallStatus === 'FAIL') return 'Mismatch Detected';
    if (this.overallStatus === 'REVIEW') return 'Review Required';
    return 'Missing Supporting Documents';
  }

  getBannerSubtitle(): string {
    const action = this.latestReviewAction;
    if (action === 'returned') return 'Sent back to Finance — awaiting correction';
    if (action === 'need_review') return 'Marked by Auditor for further review before a final decision';
    if (this.hasMaterialFinding) return this.materialFindingsPanelText;
    if (this.overallStatus === 'PASS') return 'Ready for approval';
    if (this.overallStatus === 'FAIL') return 'Review required — see highlighted rows';
    if (this.overallStatus === 'REVIEW') return 'Some fields differ — see highlighted rows';
    if (this.comparison && !this.comparison.po && !this.comparison.gr) return 'Awaiting Finance submission — PO and GR required';
    if (this.comparison && !this.comparison.po) return 'Awaiting Finance submission — PO required';
    if (this.comparison && !this.comparison.gr) return 'Awaiting Finance submission — GR required';
    return 'Awaiting Finance action';
  }

  // ── Exception Summary / Audit Impact / Suggested Action ─────────
  // Display-only classification derived purely from `comparison`
  // (already loaded for the Field Comparison table below — no new API
  // call, nothing fabricated). Mirrors the SAME severity/priority
  // reasoning routes/auditor.py::_classify_exception() already uses
  // (mismatch outranks a missing document) without calling or
  // duplicating that backend logic — this is presentation only, for
  // the ONE record already open on this page.

  get missingDocs(): string[] {
    if (!this.comparison) return [];
    const missing: string[] = [];
    if (!this.comparison.po) missing.push('Purchase Order');
    if (!this.comparison.gr) missing.push('Goods Receipt');
    return missing;
  }

  get isMissingDocumentsIssue(): boolean {
    return this.overallStatus === 'PARTIAL' && this.missingDocs.length > 0;
  }

  get mismatchedFields(): string[] {
    if (!this.comparison) return [];
    const mr = this.comparison.match_result;
    const fields: string[] = [];
    if (mr.vendor_match === false) fields.push('Vendor / Supplier');
    if (mr.amount_match === false) fields.push('Amount');
    if (mr.line_items_match === false) fields.push('Line Items');
    if (mr.po_reference_match === false) fields.push('PO Reference');
    if (mr.line_items_price_match === false) fields.push('Line Item Amount');
    return fields;
  }

  // '' means "no exception to summarize" — the Exception Summary card
  // is hidden entirely for a clean PASS record rather than showing an
  // empty/meaningless card.
  get exceptionIssueTitle(): string {
    if (this.overallStatus === 'FAIL') return 'Field Mismatch Detected';
    if (this.overallStatus === 'REVIEW') return 'Fields Require Review';
    if (this.isMissingDocumentsIssue) return 'Missing Supporting Documents';
    return '';
  }

  get exceptionRiskLevel(): string {
    if (this.overallStatus === 'FAIL') return 'High';
    if (this.overallStatus === 'REVIEW') return 'Medium';
    if (this.isMissingDocumentsIssue) return 'Medium';
    return '';
  }

  exceptionRiskClass(level: string): string {
    if (level === 'High') return 'risk-high';
    if (level === 'Medium') return 'risk-medium';
    return 'risk-low';
  }

  // Reuses the same risk-chip color classes as exceptionRiskClass above —
  // PASS reads as "low risk" (green), REVIEW REQUIRED as "needs attention"
  // (amber) — no new CSS needed.
  auditStatusClass(status: string): string {
    return status === 'PASS' ? 'risk-low' : 'risk-medium';
  }

  get evidenceListLabel(): string {
    if (this.isMissingDocumentsIssue) return 'Missing';
    if (this.overallStatus === 'FAIL' || this.overallStatus === 'REVIEW') return 'Affected Fields';
    return '';
  }

  get evidenceList(): string[] {
    if (this.isMissingDocumentsIssue) return this.missingDocs;
    if (this.overallStatus === 'FAIL' || this.overallStatus === 'REVIEW') return this.mismatchedFields;
    return [];
  }

  // Short line for the Exception Summary card.
  get exceptionImpactShort(): string {
    if (this.overallStatus === 'FAIL') return 'Invoice cannot be reliably matched against PO/GR records.';
    if (this.overallStatus === 'REVIEW') return 'Some fields differ across documents and need verification.';
    if (this.isMissingDocumentsIssue) return 'Three-way matching cannot be completed.';
    return '';
  }

  // Fuller sentence for the standalone Audit Impact card.
  get auditImpact(): string {
    if (this.overallStatus === 'FAIL') {
      return 'Invoice approval cannot be fully validated because key fields do not match across Invoice, PO and GR.';
    }
    if (this.overallStatus === 'REVIEW') {
      return 'Invoice approval cannot be fully validated until the differing fields are reviewed and confirmed.';
    }
    if (this.isMissingDocumentsIssue) {
      return 'Invoice approval cannot be fully validated because supporting documents are incomplete.';
    }
    return '';
  }

  get suggestedAction(): string {
    if (this.isMissingDocumentsIssue) {
      return `Request Finance team to provide the missing ${this.missingDocs.join(' and ')} before approval.`;
    }
    if (this.overallStatus === 'FAIL') {
      return 'Verify the mismatched fields with Finance or the vendor. Send the record back for correction if the discrepancy cannot be explained.';
    }
    if (this.overallStatus === 'REVIEW') {
      return 'Review the differing fields closely. Approve only if the difference is explainable, otherwise request clarification from Finance.';
    }
    return '';
  }

  // Financial Impact (Exception Summary card) — the monetary read of
  // this exception. Deliberately reuses amountCompareBasis/amountSymbol
  // (below) rather than the raw backend match_result.amount_match:
  // amount_match compares the invoice against the PRIMARY PO's FULL
  // total even for a v2/enterprise invoice that's only allocated a
  // share of a multi-invoice PO, so it can read as a mismatch even when
  // that invoice's own allocated amount genuinely matches — exactly the
  // gap amountCompareBasis already exists to close for the Field
  // Comparison table's Amount row. Reusing it here keeps this section
  // from ever contradicting that table. No new API call — every value
  // is already loaded in `comparison` for that same table. Returns null
  // (section hidden) when there's no invoice amount to report at all,
  // or when the exception has no real financial angle (amounts match,
  // no PO/GR missing).
  get financialImpact(): { headline: string; invoiceAmountText: string; poAmountLabel: string; poAmountText: string | null; varianceText: string | null } | null {
    if (!this.comparison || this.comparison.invoice.total_amount === null) return null;
    const invoiceAmountText = this.formatAmount(this.comparison.invoice.total_amount, this.comparison.invoice.currency);

    if (this.comparison.po) {
      const basis = this.amountCompareBasis;
      const sym = this.amountSymbol(this.comparison.invoice.total_amount, basis, this.comparison.invoice.currency, this.comparison.po.currency);
      if (sym === 'neq' && basis !== null) {
        const variance = Math.abs(this.comparison.invoice.total_amount - basis);
        return {
          headline: this.isV2Allocated
            ? 'Invoice total does not match its allocated share of the Purchase Order.'
            : 'Invoice total does not match the Purchase Order total.',
          invoiceAmountText,
          poAmountLabel: this.isV2Allocated ? 'PO (Allocated)' : 'PO',
          poAmountText: this.formatAmount(basis, this.comparison.po.currency),
          varianceText: this.formatAmount(variance, this.comparison.invoice.currency),
        };
      }
    }

    if (this.isMissingDocumentsIssue) {
      return {
        headline: `Full invoice value is unverified pending the missing ${this.missingDocs.join(' and ')}.`,
        invoiceAmountText,
        poAmountLabel: '',
        poAmountText: null,
        varianceText: null,
      };
    }

    return null;
  }

  // Shown inside the Send Back modal (Feature 5) only for the missing-
  // document case, matching the task's own example wording exactly —
  // UI guidance only, the auditor can still change every field.
  get sendBackContextNote(): string {
    if (!this.isMissingDocumentsIssue) return '';
    return this.missingDocs.join(' and ');
  }

  // amountsEqual/amountSymbol/isV2Allocated/amountCompareBasis are kept
  // here (also duplicated in auditor-matching-details.component.ts, the
  // page that now owns the full field-comparison table) because
  // financialImpact above reuses this SAME math so its number can never
  // contradict that table — see financialImpact's own comment. Every
  // OTHER field-comparison-table-only helper (vendor/row-match pills,
  // line-item cell logic, formatQuantity, the Three-way Match card's
  // fulfilment summary) moved to that page and isn't duplicated here.

  private amountsEqual(a: number | null | undefined, b: number | null | undefined): boolean {
    if (a === null || a === undefined || b === null || b === undefined) return false;
    return Math.abs(Number(a) - Number(b)) < 0.01;
  }

  amountSymbol(fromVal: number | null, toVal: number | null, fromCurrency?: string | null, toCurrency?: string | null): 'eq' | 'neq' | 'na' {
    if (fromVal === null || fromVal === undefined || toVal === null || toVal === undefined) return 'na';
    if (fromCurrency && toCurrency && fromCurrency.toUpperCase() !== toCurrency.toUpperCase()) return 'na';
    return this.amountsEqual(fromVal, toVal) ? 'eq' : 'neq';
  }

  get isV2Allocated(): boolean {
    return this.comparison?.engine_version === 'v2' && !!this.comparison?.invoice_result;
  }

  get amountCompareBasis(): number | null {
    if (this.isV2Allocated && this.comparison.invoice_result.allocated_amount != null) {
      return this.comparison.invoice_result.allocated_amount;
    }
    return this.comparison?.po?.total_amount ?? null;
  }

  // ── Formatting ───────────────────────────────────────────

  formatAmount(amount: any, currency?: string | null): string {
    if (amount === null || amount === undefined || amount === '') return '-';
    return (currency || 'RM') + ' ' + parseFloat(amount).toLocaleString('en-MY', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  formatDate(dateStr: string): string {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleDateString('en-MY', {
      day: '2-digit', month: 'short', year: 'numeric'
    });
  }

}
