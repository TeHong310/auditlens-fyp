import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

// Reusable placeholder for a real system screenshot that hasn't been
// captured yet (User Manual). Deliberately never generates or fakes an
// image — just a clearly-labeled empty box ready to be swapped for a
// real screenshot later.
@Component({
  selector: 'app-screenshot-placeholder',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="screenshot-placeholder">
      <i class="ph-duotone ph-image"></i>
      <p>[ Screenshot Placeholder ]</p>
      <span>{{ label }}</span>
    </div>
  `,
  styles: [`
    .screenshot-placeholder {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 28px 16px;
      border: 1.5px dashed var(--border-hover);
      border-radius: 10px;
      background: var(--bg-hover);
      color: var(--text-muted);
      text-align: center;
    }
    .screenshot-placeholder i { font-size: 28px; opacity: 0.6; }
    .screenshot-placeholder p {
      font-size: 13px;
      font-weight: 600;
      margin: 0;
      color: var(--text-secondary);
    }
    .screenshot-placeholder span { font-size: 11.5px; }
  `]
})
export class ScreenshotPlaceholderComponent {
  @Input() label: string = 'Add real system screenshot here';
}
