"""Regression tests for the Document Workflow Timeline feature
(routes/documents.py::_build_timeline_events/_require_timeline_access/
get_document_timeline) — a pure visualization layer reused by both the
Auditor Record Detail page and the Finance Correction Detail page.

No real DB, no real Claude/Gemini calls, no real Flask request/JWT
dispatch — every external call is monkey-patched with fakes/stubs, same
style as tests/extraction/test_ai_assistant.py. _build_timeline_events()
is a pure function (dict in, list out) so most cases here call it
directly with a hand-built context dict rather than going through the
full route.

Usage:
    python tests/extraction/test_document_timeline.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask

import routes.documents as rd

FAILURES = []

# _require_timeline_access() calls jsonify() on the rejection path,
# which needs a Flask application context — a minimal throwaway app is
# enough (no blueprint registration, no real DB).
_test_app = Flask(__name__)


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


class _Patched:
    """Context manager that monkey-patches a module's attributes for the
    duration of the `with` block and restores them afterward — same
    pattern used throughout tests/extraction/."""

    def __init__(self, module, **overrides):
        self.module = module
        self.overrides = overrides
        self._originals = {}

    def __enter__(self):
        for name, value in self.overrides.items():
            self._originals[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._originals.items():
            setattr(self.module, name, value)


def _event(events, name):
    return next((e for e in events if e['event'] == name), None)


# ============================================================
# _build_timeline_events() — the 4 scenarios the task explicitly asks
# to verify, plus a few individual-step edge cases.
# ============================================================

def _base_context(**overrides):
    """A minimal, fully-populated context dict matching the shape
    _build_case_context() actually returns — each test overrides only
    the fields relevant to the scenario under test."""
    ctx = {
        'invoice_number': 'INV-1', 'vendor': 'Vendor A', 'amount': 100.0,
        'currency': 'RM', 'invoice_date': '2026-07-01',
        'uploaded_at': '2026-07-22T09:10:00', 'ocr_confidence': 96.23,
        'po_uploaded': True, 'gr_uploaded': True, 'missing_documents': [],
        'matching_status': 'PASS',
        'matching_details': {},
        'exception': None,
        'authenticity': {'invoice': {'status': 'passed', 'risk_level': 'low'}},
        'anomalies': [],
        'audit_history': [],
        'document_status': 'under_review',
        'audit_status': 'PASS',
        'audit_status_reasons': ['All core checks passed and no blocking findings'],
        'send_back_cycle': None,
    }
    ctx.update(overrides)
    return ctx


def run_case_fully_approved_invoice():
    print('Case 1/4: Fully approved invoice -> every step completed, no Finance Correction step')
    context = _base_context(
        document_status='approved',
        audit_history=[{'action': 'approved', 'remarks': 'Looks good', 'reviewed_at': '2026-07-23T10:00:00', 'reviewer': 'Auditor One'}],
    )
    events = rd._build_timeline_events(context)

    check('document_uploaded is completed', _event(events, 'document_uploaded')['status'] == 'completed', events)
    check('ocr_extraction is completed', _event(events, 'ocr_extraction')['status'] == 'completed', events)
    check('three_way_matching is completed (PASS)', _event(events, 'three_way_matching')['status'] == 'completed', events)
    check('authenticity_verification is completed', _event(events, 'authenticity_verification')['status'] == 'completed', events)
    check('anomaly_evaluation is completed (no anomalies)', _event(events, 'anomaly_evaluation')['status'] == 'completed', events)
    check('auditor_review is completed (approved)', _event(events, 'auditor_review')['status'] == 'completed', events)
    check('auditor_review detail is "Approved"', _event(events, 'auditor_review')['detail'] == 'Approved', events)
    check('final_approval is completed', _event(events, 'final_approval')['status'] == 'completed', events)
    check('no Finance Correction step (never returned)', _event(events, 'finance_correction') is None, events)


def run_case_missing_po_gr_invoice():
    print('Case 2/4: Missing PO/GR invoice -> matching is action_required, auditor review is pending')
    context = _base_context(
        po_uploaded=False, gr_uploaded=False,
        missing_documents=['Purchase Order', 'Goods Receipt'],
        matching_status='PARTIAL',
        document_status='under_review',
        audit_history=[],
    )
    events = rd._build_timeline_events(context)

    mt = _event(events, 'three_way_matching')
    check('three_way_matching is action_required (missing documents)', mt['status'] == 'action_required', mt)
    check('detail names the missing documents', 'Purchase Order' in mt['detail'] and 'Goods Receipt' in mt['detail'], mt)
    check('auditor_review is pending (no action taken yet)', _event(events, 'auditor_review')['status'] == 'pending', events)
    check('final_approval is pending', _event(events, 'final_approval')['status'] == 'pending', events)
    check('no Finance Correction step (never returned)', _event(events, 'finance_correction') is None, events)


def run_case_returned_invoice():
    print('Case 3/4: Returned invoice -> auditor_review action_required with reason, finance_correction awaiting submission')
    context = _base_context(
        po_uploaded=False, gr_uploaded=False, missing_documents=['Purchase Order', 'Goods Receipt'],
        matching_status='PARTIAL',
        document_status='returned',
        audit_history=[{'action': 'returned', 'remarks': 'Missing Purchase Order and Goods Receipt',
                         'reviewed_at': '2026-07-22T11:00:00', 'reviewer': 'Auditor One'}],
        send_back_cycle={
            'reason_category': 'missing_document', 'reason_other_note': None,
            'auditor_instruction': 'Missing Purchase Order and Goods Receipt',
            'required_actions': ['upload_missing_document'], 'priority': 'medium',
            'response_due_date': None, 'cycle_status': 'action_required',
            'sent_back_at': '2026-07-22T11:00:00',
        },
    )
    events = rd._build_timeline_events(context)

    ar = _event(events, 'auditor_review')
    check('auditor_review is action_required', ar['status'] == 'action_required', ar)
    check('auditor_review detail is "Returned to Finance"', ar['detail'] == 'Returned to Finance', ar)
    check('auditor_review reason is the auditor instruction', ar['reason'] == 'Missing Purchase Order and Goods Receipt', ar)

    fc = _event(events, 'finance_correction')
    check('finance_correction step exists', fc is not None, events)
    check('finance_correction is action_required', fc['status'] == 'action_required', fc)
    check('finance_correction detail is "Awaiting submission"', fc['detail'] == 'Awaiting submission', fc)

    check('final_approval is still pending', _event(events, 'final_approval')['status'] == 'pending', events)


def run_case_finance_corrected_invoice():
    print('Case 4/4: Finance-corrected (resubmitted) invoice -> finance_correction completed, still awaiting final approval')
    context = _base_context(
        document_status='resubmitted',
        audit_history=[
            {'action': 'returned', 'remarks': 'Missing Purchase Order and Goods Receipt',
             'reviewed_at': '2026-07-22T11:00:00', 'reviewer': 'Auditor One'},
            {'action': 'resubmitted', 'remarks': 'Documents uploaded', 'reviewed_at': '2026-07-22T15:00:00', 'reviewer': 'Finance User'},
        ],
        send_back_cycle={
            'reason_category': 'missing_document', 'reason_other_note': None,
            'auditor_instruction': 'Missing Purchase Order and Goods Receipt',
            'required_actions': ['upload_missing_document'], 'priority': 'medium',
            'response_due_date': None, 'cycle_status': 'resubmitted',
            'sent_back_at': '2026-07-22T11:00:00',
        },
    )
    events = rd._build_timeline_events(context)

    # 'resubmitted' is a FINANCE action, not an auditor one — the
    # auditor_review step still reflects the ORIGINAL 'returned' action,
    # never conflated with the Finance Correction step below it.
    ar = _event(events, 'auditor_review')
    check('auditor_review still reflects the original "returned" action', ar['detail'] == 'Returned to Finance', ar)

    fc = _event(events, 'finance_correction')
    check('finance_correction is completed', fc['status'] == 'completed', fc)
    check('finance_correction detail is "Completed"', fc['detail'] == 'Completed', fc)

    check('final_approval is still pending (awaiting auditor re-review)',
          _event(events, 'final_approval')['status'] == 'pending', events)


# ============================================================
# Individual-step edge cases not covered by the 4 scenarios above
# ============================================================

def run_case_ocr_not_yet_done_is_pending():
    print('Case: no ocr_confidence yet -> OCR Extraction is pending, not action_required')
    context = _base_context(ocr_confidence=None)
    events = rd._build_timeline_events(context)
    ocr = _event(events, 'ocr_extraction')
    check('ocr_extraction is pending', ocr['status'] == 'pending', ocr)


def run_case_authenticity_not_yet_checked_is_pending():
    print('Case: no authenticity check yet -> Authenticity Verification is pending, not action_required')
    context = _base_context(authenticity=None)
    events = rd._build_timeline_events(context)
    auth = _event(events, 'authenticity_verification')
    check('authenticity_verification is pending', auth['status'] == 'pending', auth)


def run_case_authenticity_warning_is_action_required():
    print('Case: authenticity status "warning" -> Authenticity Verification is action_required')
    context = _base_context(authenticity={'invoice': {'status': 'warning', 'risk_level': 'medium'}})
    events = rd._build_timeline_events(context)
    auth = _event(events, 'authenticity_verification')
    check('authenticity_verification is action_required', auth['status'] == 'action_required', auth)
    check('detail says "Warning"', auth['detail'] == 'Status: Warning', auth)


def run_case_blocking_anomaly_is_action_required():
    print('Case: a pending high-severity anomaly -> Anomaly Evaluation is action_required')
    context = _base_context(anomalies=[
        {'anomaly_type': 'duplicate', 'severity': 'high', 'status': 'pending', 'classification': 'blocking'}
    ])
    events = rd._build_timeline_events(context)
    an = _event(events, 'anomaly_evaluation')
    check('anomaly_evaluation is action_required', an['status'] == 'action_required', an)
    check('detail says "Review Required"', an['detail'] == 'Status: Review Required', an)


def run_case_informational_only_anomaly_does_not_block():
    print('Case: an already-reviewed (informational) anomaly does NOT block Anomaly Evaluation')
    context = _base_context(anomalies=[
        {'anomaly_type': 'duplicate', 'severity': 'medium', 'status': 'reviewed', 'classification': 'informational'}
    ])
    events = rd._build_timeline_events(context)
    an = _event(events, 'anomaly_evaluation')
    check('anomaly_evaluation stays completed despite the informational finding', an['status'] == 'completed', an)
    check('detail says "No Blocking Issue"', an['detail'] == 'Status: No Blocking Issue', an)


def run_case_review_mismatch_is_action_required():
    print('Case: matching_status REVIEW -> Three-way Matching is action_required')
    context = _base_context(matching_status='REVIEW')
    events = rd._build_timeline_events(context)
    mt = _event(events, 'three_way_matching')
    check('three_way_matching is action_required', mt['status'] == 'action_required', mt)


def run_case_need_review_auditor_action_is_action_required():
    print('Case: latest auditor action is "need_review" -> auditor_review is action_required')
    context = _base_context(audit_history=[
        {'action': 'need_review', 'remarks': None, 'reviewed_at': '2026-07-22T12:00:00', 'reviewer': 'Auditor One'}
    ])
    events = rd._build_timeline_events(context)
    ar = _event(events, 'auditor_review')
    check('auditor_review is action_required', ar['status'] == 'action_required', ar)
    check('detail mentions further review', ar['detail'] == 'Marked for further review', ar)


# ============================================================
# routes/documents.py — _require_timeline_access()
# ============================================================

class _OwnerCursor:
    def __init__(self, uploaded_by, doc_exists=True):
        self.uploaded_by = uploaded_by
        self.doc_exists = doc_exists

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return {'uploaded_by': self.uploaded_by} if self.doc_exists else None


class _OwnerConn:
    def __init__(self, uploaded_by, doc_exists=True):
        self._cursor = _OwnerCursor(uploaded_by, doc_exists)

    def cursor(self, **kwargs):
        return self._cursor

    def close(self):
        pass


def run_case_timeline_access_auditor_unrestricted():
    print('Case: an auditor is granted access without any ownership lookup')
    db_calls = []
    with _test_app.app_context(), _Patched(
            rd,
            get_jwt_identity=lambda: 1,
            get_user_by_id=lambda uid: {'user_id': 1, 'role': 'auditor'},
            get_db_connection=lambda: db_calls.append(1) or _OwnerConn(uploaded_by=999)):
        user, err = rd._require_timeline_access(5)
    check('auditor accepted (no error)', err is None, err)
    check('no DB ownership lookup performed for an auditor', db_calls == [], db_calls)


def run_case_timeline_access_finance_owner_accepted():
    print('Case: a finance_executive who uploaded this document is accepted')
    with _test_app.app_context(), _Patched(
            rd,
            get_jwt_identity=lambda: 5,
            get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
            get_db_connection=lambda: _OwnerConn(uploaded_by=5)):
        user, err = rd._require_timeline_access(1)
    check('owner accepted (no error)', err is None, err)


def run_case_timeline_access_finance_non_owner_rejected():
    print('Case: a finance_executive who did NOT upload this document is rejected (403)')
    with _test_app.app_context(), _Patched(
            rd,
            get_jwt_identity=lambda: 5,
            get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
            get_db_connection=lambda: _OwnerConn(uploaded_by=99)):
        user, err = rd._require_timeline_access(1)
    check('rejected with 403', err is not None and err[1] == 403, err)


def run_case_timeline_access_missing_document_404():
    print('Case: a nonexistent document (for a finance user) returns 404')
    with _test_app.app_context(), _Patched(
            rd,
            get_jwt_identity=lambda: 5,
            get_user_by_id=lambda uid: {'user_id': 5, 'role': 'finance_executive'},
            get_db_connection=lambda: _OwnerConn(uploaded_by=None, doc_exists=False)):
        user, err = rd._require_timeline_access(999)
    check('rejected with 404', err is not None and err[1] == 404, err)


def run_case_timeline_access_rejects_unknown_role():
    print('Case: a role that is neither auditor nor finance_executive is rejected (403)')
    with _test_app.app_context(), _Patched(
            rd,
            get_jwt_identity=lambda: 9,
            get_user_by_id=lambda uid: {'user_id': 9, 'role': 'admin'}):
        user, err = rd._require_timeline_access(1)
    check('rejected with 403', err is not None and err[1] == 403, err)


if __name__ == '__main__':
    run_case_fully_approved_invoice()
    run_case_missing_po_gr_invoice()
    run_case_returned_invoice()
    run_case_finance_corrected_invoice()

    run_case_ocr_not_yet_done_is_pending()
    run_case_authenticity_not_yet_checked_is_pending()
    run_case_authenticity_warning_is_action_required()
    run_case_blocking_anomaly_is_action_required()
    run_case_informational_only_anomaly_does_not_block()
    run_case_review_mismatch_is_action_required()
    run_case_need_review_auditor_action_is_action_required()

    run_case_timeline_access_auditor_unrestricted()
    run_case_timeline_access_finance_owner_accepted()
    run_case_timeline_access_finance_non_owner_rejected()
    run_case_timeline_access_missing_document_404()
    run_case_timeline_access_rejects_unknown_role()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S):')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    else:
        print('All checks passed.')
        sys.exit(0)
