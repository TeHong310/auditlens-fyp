import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

// Reusable slot for a User Manual screenshot. `src` names where a
// developer-provided screenshot WOULD live (e.g. "manual/review-queue.
// png", served from public/manual/ — see that folder's own README).
// This component never uploads, generates, or fakes an image — it only
// ever displays a file that's already sitting in public/manual/ at
// build time. The <img> always attempts to load in the background
// (display:none) so the dashed placeholder box is what's visible
// unless/until that load actually succeeds — a missing file (the
// common case, since most sections have no screenshot yet) silently
// keeps showing the placeholder, never a broken-image icon.
@Component({
  selector: 'app-screenshot-placeholder',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="screenshot-slot">
      <img *ngIf="src"
           [src]="src"
           [alt]="label"
           class="manual-screenshot"
           [style.display]="loaded ? 'block' : 'none'"
           (load)="loaded = true"
           (error)="loaded = false" />
      <div class="screenshot-placeholder" *ngIf="!loaded">
        <i class="ph-duotone ph-image"></i>
        <p>[ Screenshot Placeholder ]</p>
        <span>{{ label }}</span>
      </div>
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

    .manual-screenshot {
      display: block;
      width: 100%;
      max-height: 600px;
      object-fit: contain;
      border-radius: 12px;
      border: 1px solid var(--border-hover);
      background: var(--bg-hover);
    }
  `]
})
export class ScreenshotPlaceholderComponent {
  @Input() label: string = 'Add real system screenshot here';
  // Path under public/ where a developer-provided screenshot goes
  // (e.g. "manual/review-queue.png"). Omitted entirely = always shows
  // the placeholder, for sections that don't have a designated slot.
  @Input() src?: string;
  loaded = false;
}
