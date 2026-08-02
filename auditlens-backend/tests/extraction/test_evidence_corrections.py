"""Regression tests for helpers/evidence_corrections.py — v10 Human-in-
the-Loop evidence-location editing. No real DB writes: get_db_connection
is monkey-patched with an in-memory fake (same house style as
test_authenticity_siblings.py) that faithfully reproduces the table's
UNIQUE(document_id, document_type, evidence_type) upsert semantics and
the authenticity_checks.boxes lookup apply_evidence_changes() depends
on, so the real INSERT ... ON CONFLICT DO UPDATE / DELETE logic in
evidence_corrections.py runs unmodified against it.

Usage:
    python tests/extraction/test_evidence_corrections.py
Exits 0 if all cases pass, 1 if any fail.
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import helpers.evidence_corrections as ec

FAILURES = []


def check(label, condition, detail=''):
    if condition:
        print(f'  OK   {label}')
    else:
        print(f'  FAIL {label}  {detail}')
        FAILURES.append(f'{label}  {detail}')


# ══════════════════════ Fake DB layer ══════════════════════

class _FakeCursor:
    def __init__(self, store, boxes_by_doc):
        self.store = store              # {(doc_id, doc_type, evidence_type): row_dict}
        self.boxes_by_doc = boxes_by_doc  # {(doc_id, doc_type): [box_dict, ...]}
        self._result = None

    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        params = params or ()

        if s.startswith('SELECT boxes FROM authenticity_checks'):
            doc_id, doc_type = params
            boxes = self.boxes_by_doc.get((doc_id, doc_type))
            self._result = (boxes,) if boxes is not None else None

        elif s.startswith('DELETE FROM authenticity_evidence_corrections'):
            doc_id, doc_type, evidence_type = params
            self.store.pop((doc_id, doc_type, evidence_type), None)
            self._result = None

        elif "source, original_ai_box, corrected_by) VALUES (%s, %s, %s, 1, NULL" in s:
            doc_id, doc_type, evidence_type, user_id = params
            key = (doc_id, doc_type, evidence_type)
            existing_id = self.store[key]['correction_id'] if key in self.store else len(self.store) + 1
            self.store[key] = {
                'correction_id': existing_id, 'evidence_type': evidence_type, 'page': 1,
                'x': None, 'y': None, 'width': None, 'height': None,
                'source': 'auditor_deleted', 'original_ai_box': None,
                'corrected_by': user_id, 'corrected_at': datetime.datetime(2026, 1, 1),
            }
            self._result = None

        elif s.startswith('INSERT INTO authenticity_evidence_corrections'):
            doc_id, doc_type, evidence_type, page, x, y, w, h, source, ai_box, user_id = params
            key = (doc_id, doc_type, evidence_type)
            existing_id = self.store[key]['correction_id'] if key in self.store else len(self.store) + 1
            ai_box_value = ai_box.adapted if hasattr(ai_box, 'adapted') else ai_box
            self.store[key] = {
                'correction_id': existing_id, 'evidence_type': evidence_type, 'page': page,
                'x': x, 'y': y, 'width': w, 'height': h,
                'source': source, 'original_ai_box': ai_box_value,
                'corrected_by': user_id, 'corrected_at': datetime.datetime(2026, 1, 1),
            }
            self._result = None

        elif s.startswith('SELECT correction_id, evidence_type, page'):
            doc_id, doc_type = params
            rows = [dict(r) for (d, t, _e), r in self.store.items() if d == doc_id and t == doc_type]
            self._result = sorted(rows, key=lambda r: r['evidence_type'])

        else:
            raise AssertionError(f'Unrecognized SQL in fake cursor: {s[:80]}')

    def fetchone(self):
        return self._result if not isinstance(self._result, list) else None

    def fetchall(self):
        return self._result if isinstance(self._result, list) else []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def make_env(boxes_by_doc=None):
    """Returns (store, restore) — patches ec.get_db_connection to hand
    out a fresh fake connection backed by a shared `store` dict every
    call (matching how the real code opens a new connection per query),
    and returns a restore() to undo the patch."""
    store = {}
    boxes_by_doc = boxes_by_doc or {}
    original = ec.get_db_connection
    ec.get_db_connection = lambda: _FakeConn(_FakeCursor(store, boxes_by_doc))
    return store, (lambda: setattr(ec, 'get_db_connection', original))


# ══════════════════════ _validate_change (pure) ══════════════════════

def test_validate_evidence_type_must_be_valid_for_doc_type():
    try:
        ec._validate_change('invoice', {'evidence_type': 'supplier_address', 'action': 'delete'})
        check('invoice: supplier_address is rejected (not a valid invoice evidence type)', False)
    except ec.EvidenceValidationError:
        check('invoice: supplier_address is rejected (not a valid invoice evidence type)', True)

    try:
        ec._validate_change('po', {'evidence_type': 'buyer_received_stamp', 'action': 'delete'})
        check('po: buyer_received_stamp is rejected (invoice-only type)', False)
    except ec.EvidenceValidationError:
        check('po: buyer_received_stamp is rejected (invoice-only type)', True)

    # valid for its type -> no exception
    ec._validate_change('gr', {'evidence_type': 'handwritten_notes', 'action': 'delete'})
    check('gr: handwritten_notes accepted (valid GR-only type)', True)


def test_validate_unknown_document_type():
    try:
        ec._validate_change('shipping_label', {'evidence_type': 'supplier_name', 'action': 'delete'})
        check('unknown document_type rejected', False)
    except ec.EvidenceValidationError:
        check('unknown document_type rejected', True)


def test_validate_unknown_action():
    try:
        ec._validate_change('invoice', {'evidence_type': 'supplier_name', 'action': 'teleport'})
        check('unknown action rejected', False)
    except ec.EvidenceValidationError:
        check('unknown action rejected', True)


def test_validate_zero_or_negative_dimensions_rejected():
    for w, h in [(0, 0.1), (0.1, 0), (-0.1, 0.1), (0.1, -0.1)]:
        try:
            ec._validate_change('invoice', {
                'evidence_type': 'supplier_name', 'action': 'correct', 'page': 1,
                'x': 0.1, 'y': 0.1, 'width': w, 'height': h,
            })
            check(f'zero/negative box (w={w}, h={h}) rejected', False)
        except ec.EvidenceValidationError:
            check(f'zero/negative box (w={w}, h={h}) rejected', True)


def test_validate_coordinates_outside_image_rejected():
    cases = [
        {'x': -0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2},   # x negative
        {'x': 0.1, 'y': -0.1, 'width': 0.2, 'height': 0.2},   # y negative
        {'x': 0.9, 'y': 0.1, 'width': 0.3, 'height': 0.2},    # x+width > 1
        {'x': 0.1, 'y': 0.9, 'width': 0.2, 'height': 0.3},    # y+height > 1
    ]
    for c in cases:
        change = {'evidence_type': 'supplier_name', 'action': 'add', 'page': 1, **c}
        try:
            ec._validate_change('invoice', change)
            check(f'out-of-image box {c} rejected', False)
        except ec.EvidenceValidationError:
            check(f'out-of-image box {c} rejected', True)


def test_validate_valid_box_accepted():
    ec._validate_change('invoice', {
        'evidence_type': 'buyer_name', 'action': 'add', 'page': 1,
        'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.1,
    })
    check('a well-formed box within [0,1] is accepted', True)
    # exactly touching the edges (x=0, x+width=1) must still be valid
    ec._validate_change('invoice', {
        'evidence_type': 'buyer_name', 'action': 'add', 'page': 1,
        'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0,
    })
    check('a box exactly filling the page (0..1) is accepted', True)


def test_validate_delete_and_reset_need_no_coordinates():
    ec._validate_change('invoice', {'evidence_type': 'buyer_name', 'action': 'delete'})
    ec._validate_change('invoice', {'evidence_type': 'buyer_name', 'action': 'reset'})
    check('delete/reset actions need no x/y/width/height', True)


# ══════════════════════ apply_evidence_changes / get_corrections_for ══════════════════════

def test_add_correction_when_no_ai_box_exists():
    store, restore = make_env(boxes_by_doc={})
    try:
        result = ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.1},
        ], user_id=42)
        check('add: exactly one correction returned', len(result) == 1, result)
        check('add: source is auditor_added (no AI box existed)', result[0]['source'] == 'auditor_added', result)
        check('add: original_ai_box is None', result[0]['original_ai_box'] is None, result)
        check('add: corrected_by is the acting user', result[0]['corrected_by'] == 42, result)
        check('add: x/y/width/height are floats, not Decimal', all(isinstance(result[0][k], float) for k in ('x', 'y', 'width', 'height')))
    finally:
        restore()


def test_correct_when_ai_box_exists_snapshots_it():
    ai_box = {'type': 'supplier_name', 'polygon': [{'x': 1, 'y': 1}], 'confidence': 0.9}
    store, restore = make_env(boxes_by_doc={(1, 'invoice'): [ai_box]})
    try:
        result = ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'supplier_name', 'action': 'correct', 'page': 1, 'x': 0.2, 'y': 0.2, 'width': 0.4, 'height': 0.1},
        ], user_id=7)
        check('correct: source is auditor_corrected (AI box existed)', result[0]['source'] == 'auditor_corrected', result)
        check('correct: original_ai_box snapshots the CURRENT AI box', result[0]['original_ai_box'] == ai_box, result)
    finally:
        restore()


def test_delete_stores_null_coordinates_distinct_from_no_row():
    store, restore = make_env()
    try:
        before = ec.get_corrections_for(1, 'invoice')
        check('before any change: no correction rows exist', before == [], before)

        ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'buyer_received_stamp', 'action': 'delete'},
        ], user_id=1)
        after = ec.get_corrections_for(1, 'invoice')
        check('delete: exactly one row now exists (auditor_deleted)', len(after) == 1, after)
        check('delete: source is auditor_deleted', after[0]['source'] == 'auditor_deleted', after)
        check('delete: coordinates are null', after[0]['x'] is None and after[0]['width'] is None, after)
    finally:
        restore()


def test_reset_removes_the_correction_row_entirely():
    store, restore = make_env(boxes_by_doc={(1, 'invoice'): [{'type': 'supplier_name'}]})
    try:
        ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'supplier_name', 'action': 'correct', 'page': 1, 'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.1},
        ], user_id=1)
        check('after correcting: one row exists', len(ec.get_corrections_for(1, 'invoice')) == 1)

        ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'supplier_name', 'action': 'reset'},
        ], user_id=1)
        check('after reset: the correction row is gone entirely', ec.get_corrections_for(1, 'invoice') == [])
    finally:
        restore()


def test_add_after_delete_is_auditor_added_even_though_ai_box_still_exists():
    # The AI box for this type is NEVER removed from authenticity_checks
    # .boxes just because the auditor deleted their view of it — a
    # fresh 'add' after a 'delete' must still be honestly reported as
    # auditor_added (the auditor explicitly rejected the AI's region),
    # not silently relabeled auditor_corrected just because that old AI
    # box still technically exists in the data.
    ai_box = {'type': 'buyer_received_stamp', 'polygon': 'still-here'}
    store, restore = make_env(boxes_by_doc={(1, 'invoice'): [ai_box]})
    try:
        ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'buyer_received_stamp', 'action': 'delete'},
        ], user_id=1)
        check('after delete: source is auditor_deleted', ec.get_corrections_for(1, 'invoice')[0]['source'] == 'auditor_deleted')

        # Frontend decided this is an 'add' (nothing was displayed to
        # correct — the row showed "Needs Location").
        result = ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'buyer_received_stamp', 'action': 'add', 'page': 1,
             'x': 0.3, 'y': 0.3, 'width': 0.2, 'height': 0.1},
        ], user_id=1)
        check('a fresh draw after delete is auditor_added, not auditor_corrected',
              result[0]['source'] == 'auditor_added', result)
    finally:
        restore()


def test_upsert_latest_correction_wins():
    store, restore = make_env()
    try:
        ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.1},
        ], user_id=1)
        ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.5, 'y': 0.5, 'width': 0.2, 'height': 0.1},
        ], user_id=2)
        rows = ec.get_corrections_for(1, 'invoice')
        check('re-saving the SAME evidence_type upserts (still exactly 1 row)', len(rows) == 1, rows)
        check('the latest x/corrected_by wins', rows[0]['x'] == 0.5 and rows[0]['corrected_by'] == 2, rows)
    finally:
        restore()


def test_invalid_change_in_batch_rejects_the_whole_batch():
    store, restore = make_env()
    try:
        try:
            ec.apply_evidence_changes(1, 'invoice', [
                {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.1},
                {'evidence_type': 'supplier_name', 'action': 'add', 'page': 1, 'x': 0.1, 'y': 0.1, 'width': -1, 'height': 0.1},
            ], user_id=1)
            check('invalid entry in a batch raises before writing anything', False)
        except ec.EvidenceValidationError:
            check('invalid entry in a batch raises before writing anything', True)
        check('NOTHING from the batch was persisted (atomic reject)', ec.get_corrections_for(1, 'invoice') == [], store)
    finally:
        restore()


def test_documents_are_isolated_from_each_other():
    store, restore = make_env()
    try:
        ec.apply_evidence_changes(1, 'invoice', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.1},
        ], user_id=1)
        ec.apply_evidence_changes(2, 'invoice', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.3, 'y': 0.3, 'width': 0.2, 'height': 0.1},
        ], user_id=1)
        check('document 1 has its own correction', len(ec.get_corrections_for(1, 'invoice')) == 1)
        check('document 2 has its own, separate correction', len(ec.get_corrections_for(2, 'invoice')) == 1)
        check('the two documents do not share/overwrite each other',
              ec.get_corrections_for(1, 'invoice')[0]['x'] == 0.1 and ec.get_corrections_for(2, 'invoice')[0]['x'] == 0.3)
    finally:
        restore()


def test_document_types_for_same_document_id_are_isolated():
    # A PO and Invoice sharing the same document_id must not collide —
    # document_type is part of the UNIQUE key alongside evidence_type.
    store, restore = make_env()
    try:
        ec.apply_evidence_changes(5, 'invoice', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.1},
        ], user_id=1)
        ec.apply_evidence_changes(5, 'po', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.4, 'y': 0.4, 'width': 0.2, 'height': 0.1},
        ], user_id=1)
        check('invoice correction for doc 5 unaffected by the PO save', ec.get_corrections_for(5, 'invoice')[0]['x'] == 0.1)
        check('po correction for doc 5 is separate', ec.get_corrections_for(5, 'po')[0]['x'] == 0.4)
    finally:
        restore()


def test_correction_survives_a_recheck_that_regenerates_ai_boxes():
    # run_authenticity_check() REPLACES authenticity_checks.boxes on
    # every recheck (a fresh Claude/Vision/OpenCV run) but never imports
    # or touches this module at all — simulated here by saving a
    # correction, then swapping in a brand-new AI boxes_by_doc payload
    # (as if a recheck just ran) and confirming the correction is
    # completely unaffected.
    store, restore = make_env(boxes_by_doc={(9, 'invoice'): [{'type': 'supplier_name', 'polygon': 'old'}]})
    try:
        ec.apply_evidence_changes(9, 'invoice', [
            {'evidence_type': 'buyer_name', 'action': 'add', 'page': 1, 'x': 0.2, 'y': 0.2, 'width': 0.3, 'height': 0.1},
        ], user_id=3)
        before = ec.get_corrections_for(9, 'invoice')
        check('correction saved before the simulated recheck', len(before) == 1, before)

        # Simulate a recheck: the AI boxes for this document are now
        # completely different (fresh Claude/Vision run).
        store_boxes = {(9, 'invoice'): [{'type': 'supplier_name', 'polygon': 'brand-new-after-recheck'}]}
        ec.get_db_connection = lambda: _FakeConn(_FakeCursor(store, store_boxes))

        after = ec.get_corrections_for(9, 'invoice')
        check('correction is UNCHANGED after AI boxes were regenerated', after == before, (before, after))
        check('correction x/y/width/height still exactly what the auditor saved',
              after[0]['x'] == 0.2 and after[0]['width'] == 0.3, after)
    finally:
        restore()


def test_empty_changes_list_is_a_noop():
    store, restore = make_env()
    try:
        result = ec.apply_evidence_changes(1, 'invoice', [], user_id=1)
        check('an empty changes list returns the (empty) current corrections, writes nothing', result == [])
    finally:
        restore()


if __name__ == '__main__':
    test_validate_evidence_type_must_be_valid_for_doc_type()
    test_validate_unknown_document_type()
    test_validate_unknown_action()
    test_validate_zero_or_negative_dimensions_rejected()
    test_validate_coordinates_outside_image_rejected()
    test_validate_valid_box_accepted()
    test_validate_delete_and_reset_need_no_coordinates()
    test_add_correction_when_no_ai_box_exists()
    test_correct_when_ai_box_exists_snapshots_it()
    test_delete_stores_null_coordinates_distinct_from_no_row()
    test_reset_removes_the_correction_row_entirely()
    test_add_after_delete_is_auditor_added_even_though_ai_box_still_exists()
    test_upsert_latest_correction_wins()
    test_invalid_change_in_batch_rejects_the_whole_batch()
    test_documents_are_isolated_from_each_other()
    test_document_types_for_same_document_id_are_isolated()
    test_correction_survives_a_recheck_that_regenerates_ai_boxes()
    test_empty_changes_list_is_a_noop()

    print('\n' + '=' * 60)
    print(f'{len(FAILURES)} FAILED' if FAILURES else 'ALL PASSED')
    for f in FAILURES:
        print('FAIL:', f)
    sys.exit(1 if FAILURES else 0)
