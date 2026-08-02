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
