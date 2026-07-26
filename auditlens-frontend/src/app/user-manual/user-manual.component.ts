import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';
import { ScreenshotPlaceholderComponent } from '../shared/screenshot-placeholder/screenshot-placeholder.component';

interface FaqItem {
  id: string;
  question: string;
  answer: string;
}

// In-system, role-based user manual. Read-only/navigational — reads
// the SAME localStorage 'user' record every layout component already
// reads (see finance-layout.component.ts/auditor-layout.component.ts)
// to decide which content to show; never touches auth, roles, or any
// backend endpoint itself. Section content below is hand-written per
// role rather than data-driven, since each section's internal layout
// genuinely differs (a workflow diagram here, a field list there, a
// severity legend elsewhere) — a generic content model would be more
// machinery than this static, rarely-changing content needs.
@Component({
  selector: 'app-user-manual',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule, ScreenshotPlaceholderComponent],
  templateUrl: './user-manual.component.html',
  styleUrls: ['./user-manual.component.css']
})
export class UserManualComponent implements OnInit {
  isFinance: boolean = true;
  searchQuery: string = '';
  private expandedSections = new Set<string>();

  // Workflow diagrams (task spec) — plain step arrays rendered as a
  // vertical chain of boxes with an arrow between each pair.
  uploadWorkflowSteps: string[] = ['Upload Document', 'OCR Extraction', 'Data Validation', 'Audit Processing'];
  approvalWorkflowSteps: string[] = ['AI Assessment', 'Auditor Review', 'Final Decision'];

  financeFaqs: FaqItem[] = [
    {
      id: 'f-po-gr',
      question: 'Why do I need to upload PO and GR?',
      answer: 'AuditLens verifies every invoice by comparing it against its Purchase Order and Goods Receipt ' +
        '(three-way matching). Without both, the record shows as "Missing Supporting Documents" and the ' +
        'auditor cannot fully validate it — approval stays blocked until they are provided.'
    },
    {
      id: 'f-ocr-review',
      question: 'Why does OCR require review?',
      answer: 'OCR/AI extraction reads scanned or photographed documents automatically, but print quality, ' +
        'handwriting, or an unusual layout can occasionally cause a misread field. Reviewing the extracted ' +
        'values before they reach audit catches this early, instead of an auditor finding it later.'
    }
  ];

  auditorFaqs: FaqItem[] = [
    {
      id: 'a-not-ready',
      question: 'Why is a transaction marked Not Ready?',
      answer: 'At least one hard blocker exists for it — a missing document, a failed three-way match, an ' +
        'authenticity warning, or an unresolved high-risk/blocking anomaly. Any of these needs to be resolved ' +
        'before the case can be approved.'
    },
    {
      id: 'a-ai-approve',
      question: 'Why does AI not approve automatically?',
      answer: 'Every AI Audit Assistant action (Explain Risk, Approval Assessment, etc.) only ever drafts an ' +
        'explanation or suggestion for the auditor to read. Approving, sending back, or marking a case for ' +
        'further review is always a manual action the auditor takes themselves.'
    },
    {
      id: 'a-low-risk-anomaly',
      question: 'Why does a low-risk anomaly appear?',
      answer: 'Low-severity or already-reviewed anomalies (e.g. a weekend-dated invoice) are shown as Risk ' +
        'Context — useful background for the auditor’s judgement — but they do not block approval on ' +
        'their own the way a blocking anomaly does.'
    }
  ];

  ngOnInit() {
    if (typeof window !== 'undefined') {
      const user = JSON.parse(localStorage.getItem('user') || '{}');
      this.isFinance = user?.role === 'finance_executive';
    }
  }

  toggleSection(id: string) {
    if (this.expandedSections.has(id)) this.expandedSections.delete(id);
    else this.expandedSections.add(id);
  }

  isExpanded(id: string): boolean {
    return this.expandedSections.has(id);
  }

  // Simple substring filter over each section/FAQ's own keyword text —
  // a section or question is hidden while searching unless its text
  // contains the query. Case-insensitive, no query means everything
  // shows.
  matches(keywords: string): boolean {
    const q = this.searchQuery.trim().toLowerCase();
    if (!q) return true;
    return keywords.toLowerCase().includes(q);
  }
}
