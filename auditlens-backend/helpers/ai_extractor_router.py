"""AI extraction provider router.

Decides which AI Vision provider(s) handle field extraction for an
upload, per Config.AI_EXTRACTION_PROVIDER:

  CLAUDE (default) — call Claude first. If Claude's result is missing/
    incomplete (see _completeness_check() below), fall back to Gemini.
  GEMINI — call Gemini only, exactly as before this router existed.
  HYBRID — identical behavior to CLAUDE in this implementation: the task
    spec's own fallback conditions aren't differentiated per mode, so
    there is no separate HYBRID-only rule to apply. Kept as a distinct
    accepted value (rather than rejected/aliased away) in case a future
    change wants to give it different behavior.

routes/documents.py supplies `claude_call`/`gemini_call` as zero-arg
callables (each already wrapping its own cache-check) rather than
pre-computed results — that is the actual mechanism that keeps API
costs down: a provider that isn't needed for a given upload is never
invoked at all, not just "logged as unused".
"""
import os

AI_EXTRACTION_PROVIDER = os.environ.get('AI_EXTRACTION_PROVIDER', 'CLAUDE').strip().upper()
_VALID_PROVIDERS = ('CLAUDE', 'GEMINI', 'HYBRID')

if AI_EXTRACTION_PROVIDER not in _VALID_PROVIDERS:
    print(f"WARNING: AI_EXTRACTION_PROVIDER={AI_EXTRACTION_PROVIDER!r} is not one of "
          f"{_VALID_PROVIDERS} — defaulting to CLAUDE")
    AI_EXTRACTION_PROVIDER = 'CLAUDE'

print(f"DEBUG AI_EXTRACTION_PROVIDER loaded: {AI_EXTRACTION_PROVIDER!r}")


def _completeness_check(result):
    """Fallback-worthiness check per the spec's "Gemini fallback
    conditions": invalid JSON / API error / timeout are already
    represented as result being None by the time this runs (claude_call()
    returns None in all of those cases — see helpers/claude_extractor.py)
    — this function only needs to check the three CONTENT-based
    conditions: missing vendor_name, missing total_amount, zero
    line_items. Returns (is_complete, reason_or_None)."""
    if not result:
        return False, 'invalid JSON or empty result'
    if not result.get('vendor_name'):
        return False, 'missing vendor_name'
    if not result.get('total_amount'):
        return False, 'missing total_amount'
    if not result.get('line_items'):
        return False, 'zero line_items'
    return True, None


def route_ai_extraction(document_type, claude_call, gemini_call):
    """
    document_type: 'invoice' | 'po' | 'gr' — for logging only.
    claude_call / gemini_call: zero-arg callables, each returning a
      result dict or None. Only the callable(s) actually needed for the
      configured provider/outcome are invoked.

    Returns (result, provider_used, fallback_reason):
      result: the winning provider's dict, or None if nothing succeeded.
      provider_used: 'CLAUDE' or 'GEMINI' — which provider's result is
        being returned (needed by the caller to decide, among other
        things, whether the result can be trusted as an authenticity-
        signals source — see routes/documents.py; Claude's schema has no
        authenticity fields at all, Gemini's merged-call schema does).
      fallback_reason: why Gemini was called as a fallback, or None if
        no fallback happened (including when provider is GEMINI, since
        that's not a "fallback" — it's the configured primary).
    """
    print(f"DEBUG AI PROVIDER: {AI_EXTRACTION_PROVIDER}")

    if AI_EXTRACTION_PROVIDER == 'GEMINI':
        result = gemini_call()
        return result, 'GEMINI', None

    # CLAUDE and HYBRID: Claude first, Gemini only as a fallback.
    result = claude_call()
    vendor = result.get('vendor_name') if result else None
    amount = result.get('total_amount') if result else None
    line_items_count = len(result.get('line_items') or []) if result else 0
    print(f"DEBUG CLAUDE EXTRACTION RESULT | vendor={vendor} | amount={amount} | "
          f"line_items_count={line_items_count}")

    is_complete, reason = _completeness_check(result)
    if is_complete:
        return result, 'CLAUDE', None

    print(f"DEBUG FALLBACK TO GEMINI | reason={reason}")
    gemini_result = gemini_call()
    return gemini_result, 'GEMINI', reason
