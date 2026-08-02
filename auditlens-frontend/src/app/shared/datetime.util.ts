// The ONE shared place AuditLens converts a backend timestamp for
// display. The backend (see auditlens-backend/helpers/time_format.py)
// always returns a real timestamp as an unambiguous ISO 8601 UTC string
// ending in 'Z' — Date correctly parses that as a UTC instant
// regardless of the viewer's own machine timezone, and
// Intl.DateTimeFormat's explicit timeZone below converts it for
// display. Deliberately NOT toLocaleString() with no timeZone option
// (what every page used to write on its own) — that silently assumes
// the viewer's machine is already set to Malaysia time, and does
// nothing at all for a Z-less/ambiguous string, which is exactly how
// this bug happened in the first place.
//
// Date-only business fields (Invoice Date, PO Date, response_due_date,
// etc.) carry no time-of-day and must never go through this — they
// have their own unrelated formatDate()/formatInvoiceDate() per page,
// left untouched.

const MYT_TIMEZONE = 'Asia/Kuala_Lumpur';

const MYT_DATETIME_FORMATTER = new Intl.DateTimeFormat('en-MY', {
  timeZone: MYT_TIMEZONE,
  day: '2-digit', month: 'short', year: 'numeric',
  hour: '2-digit', minute: '2-digit', hour12: true,
});

/** "03 Aug 2026, 05:19 am" — the one canonical absolute display format
 * for a UTC timestamp, everywhere AuditLens shows a reviewed/logged/
 * uploaded/analysed time. Returns '-' for a missing or invalid value. */
export function formatMalaysiaDateTime(isoString: string | null | undefined): string {
  if (!isoString) return '-';
  const d = new Date(isoString);
  if (isNaN(d.getTime())) return '-';
  return MYT_DATETIME_FORMATTER.format(d);
}

// ── Malaysia date-key helpers — for GROUPING/BUCKETING a timestamp by
// day or month (Finance's dashboard charts, which compute their own
// "this month"/monthly-trend buckets client-side from raw document
// lists, unlike Auditor's which are pre-bucketed server-side via
// helpers/time_format.py's malaysia_date_sql()). 'en-CA' happens to
// format as plain YYYY-MM-DD, which is what makes toMalaysiaDateKey()
// a reliable, locale-formatting-free string to slice/compare — NOT
// Date.getMonth()/getFullYear()/getDate(), which use whatever timezone
// the VIEWER's own machine happens to be set to, not necessarily
// Malaysia (the frontend equivalent of the backend's UTC-vs-session-
// timezone bug this whole fix is about).

const MYT_DATE_KEY_FORMATTER = new Intl.DateTimeFormat('en-CA', {
  timeZone: MYT_TIMEZONE, year: 'numeric', month: '2-digit', day: '2-digit',
});

/** UTC ISO string (or Date) -> 'YYYY-MM-DD', the Malaysia calendar date
 * it falls on. Returns null for a missing/invalid value. */
export function toMalaysiaDateKey(value: string | Date | null | undefined): string | null {
  if (!value) return null;
  const d = typeof value === 'string' ? new Date(value) : value;
  if (isNaN(d.getTime())) return null;
  return MYT_DATE_KEY_FORMATTER.format(d);
}

/** True if `value`'s Malaysia calendar date falls in the same
 * (year, month) as `reference` (default: right now). */
export function isSameMalaysiaMonth(value: string | Date | null | undefined, reference: Date = new Date()): boolean {
  const key = toMalaysiaDateKey(value);
  const refKey = toMalaysiaDateKey(reference);
  if (!key || !refKey) return false;
  return key.slice(0, 7) === refKey.slice(0, 7);
}

/** `value`'s Malaysia calendar month as a 0-based index (0=Jan,
 * 11=Dec) — matching Date.getMonth()'s convention, so it can drop
 * straight into a `months[...]` lookup. null for a missing/invalid
 * value. */
export function malaysiaMonthIndex(value: string | Date | null | undefined): number | null {
  const key = toMalaysiaDateKey(value);
  if (!key) return null;
  return parseInt(key.slice(5, 7), 10) - 1;
}
