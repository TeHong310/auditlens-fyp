"""Audit Workflow Calendar — GET /calendar/events aggregates FOUR event
types from existing data (nothing new is detected/classified here):

  - pending_review        <- documents.status
  - finance_correction_due <- send_back_cycles (existing table)
  - exception_followup    <- routes/auditor.py's EXISTING
                              _build_comparison()/_classify_exception()
                              (imported, not reimplemented)
  - anomaly_followup      <- the `anomalies` table, as already written
                              by helpers/anomaly_detector.py

Manual Audit Task is the ONLY genuinely new piece of data (calendar_tasks
table) — a simple task an auditor/finance user creates by hand.

No extraction/matching/authenticity/anomaly-detection logic lives here or
is modified by this file; _build_comparison/_classify_exception are
imported and called exactly as routes/authenticity.py already does.
"""
from datetime import datetime, date
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit
from helpers.send_back import is_overdue
from helpers.calendar_events import (
    validate_task_payload, exception_to_calendar_event, anomaly_to_calendar_event,
)
from routes.auditor import _build_comparison, _classify_exception

calendar_bp = Blueprint('calendar', __name__)


def _iso(value):
    """Normalizes a date/datetime/None to a YYYY-MM-DD string — day-level
    granularity, since this is a monthly calendar, not a timestamp log."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _in_range(event_date_iso, start, end):
    if not event_date_iso:
        return False
    if start and event_date_iso < start:
        return False
    if end and event_date_iso > end:
        return False
    return True


# ── Per-type event builders (each reuses existing data only) ──────────

def _pending_review_events(cursor):
    cursor.execute(
        '''SELECT d.document_id, d.updated_at, d.uploaded_at, d.status,
                  ef.invoice_number, ef.vendor_name
           FROM documents d
           LEFT JOIN extracted_fields ef ON d.document_id = ef.document_id
           WHERE d.status IN ('under_review', 'resubmitted')'''
    )
    events = []
    for row in cursor.fetchall():
        anchor = row['updated_at'] or row['uploaded_at']
        events.append({
            'event_type':   'pending_review',
            'date':         _iso(anchor),
            'title':        f"Review {row['invoice_number'] or 'Invoice'}",
            'document_id':  row['document_id'],
            'invoice_no':   row['invoice_number'],
            'vendor_name':  row['vendor_name'],
            'priority':     'normal',
            'description':  'Resubmitted — awaiting auditor review' if row['status'] == 'resubmitted'
                             else 'Awaiting auditor review',
            'status':       row['status'],
        })
    return events


def _finance_correction_events(cursor, owner_user_id=None):
    where = ["sbc.cycle_status = 'action_required'"]
    params = []
    if owner_user_id is not None:
        where.append('d.uploaded_by = %s')
        params.append(owner_user_id)
    where_clause = ' AND '.join(where)

    cursor.execute(
        f'''SELECT sbc.document_id, sbc.response_due_date, sbc.priority,
                   sbc.return_reason_category, sbc.auditor_instruction,
                   sbc.cycle_status, sbc.sent_back_at,
                   ef.invoice_number, ef.vendor_name
            FROM send_back_cycles sbc
            JOIN documents d ON sbc.document_id = d.document_id
            LEFT JOIN extracted_fields ef ON sbc.document_id = ef.document_id
            WHERE {where_clause}''',
        params
    )
    events = []
    for row in cursor.fetchall():
        cycle = dict(row)
        overdue = is_overdue(cycle)
        anchor = row['response_due_date'] or (row['sent_back_at'].date() if row['sent_back_at'] else None)
        events.append({
            'event_type':       'finance_correction_due',
            'date':              _iso(anchor),
            'title':             f"Correction required for {row['invoice_number'] or 'Invoice'}",
            'document_id':       row['document_id'],
            'invoice_no':        row['invoice_number'],
            'vendor_name':       row['vendor_name'],
            'priority':          row['priority'],
            'description':       row['auditor_instruction'],
            'status':            'Overdue' if overdue else 'Awaiting Finance correction',
            'reason_category':   row['return_reason_category'],
        })
    return events


def _exception_events(cursor):
    cursor.execute(
        '''SELECT document_id, uploaded_at, status
           FROM documents
           WHERE status IN ('under_review', 'resubmitted', 'returned')'''
    )
    doc_rows = cursor.fetchall()
    events = []
    for doc_row in doc_rows:
        comparison = _build_comparison(cursor, doc_row['document_id'])
        if not comparison:
            continue
        classified = _classify_exception(cursor, doc_row, comparison)
        if not classified:
            continue
        _, exc_type, label, detail, severity = classified
        # 'sent_back' is already represented, with far richer detail, by
        # finance_correction_due (send_back_cycles) above — skip it here
        # so the same situation doesn't appear twice on the calendar.
        if exc_type == 'sent_back':
            continue
        event = exception_to_calendar_event(
            doc_row['document_id'], comparison['invoice']['invoice_no'], comparison['invoice']['vendor_name'],
            comparison['invoice']['uploaded_at'], exc_type, label, detail, severity,
        )
        event['date'] = _iso(event['date'])
        events.append(event)
    return events


def _anomaly_events(cursor):
    cursor.execute(
        '''SELECT a.invoice_document_id, a.anomaly_type, a.severity, a.ai_explanation, a.created_at,
                  ef.invoice_number, ef.vendor_name
           FROM anomalies a
           LEFT JOIN extracted_fields ef ON a.invoice_document_id = ef.document_id
           WHERE a.status = 'pending' '''
    )
    events = []
    for row in cursor.fetchall():
        event = anomaly_to_calendar_event(
            row['invoice_document_id'], row['invoice_number'], row['vendor_name'],
            row['created_at'], row['anomaly_type'], row['severity'], row['ai_explanation'],
        )
        event['date'] = _iso(event['date'])
        events.append(event)
    return events


def _manual_task_events(cursor, user_id):
    cursor.execute(
        '''SELECT ct.task_id, ct.title, ct.description, ct.event_date, ct.priority, ct.status,
                  ct.assigned_to, u1.full_name AS assigned_to_name,
                  ct.created_by, u2.full_name AS created_by_name
           FROM calendar_tasks ct
           LEFT JOIN users u1 ON ct.assigned_to = u1.user_id
           JOIN users u2 ON ct.created_by = u2.user_id
           WHERE ct.assigned_to = %s OR ct.created_by = %s
           ORDER BY ct.event_date ASC''',
        (user_id, user_id)
    )
    events = []
    for row in cursor.fetchall():
        events.append({
            'event_type':      'manual_task',
            'date':             _iso(row['event_date']),
            'title':            row['title'],
            'document_id':      None,
            'invoice_no':       None,
            'vendor_name':      None,
            'priority':         row['priority'],
            'description':      row['description'],
            'status':           row['status'],
            'task_id':          row['task_id'],
            'assigned_to':      row['assigned_to'],
            'assigned_to_name': row['assigned_to_name'],
            'created_by_name':  row['created_by_name'],
        })
    return events


# ------------------------------------------------------------
# GET AGGREGATED CALENDAR EVENTS
# GET /calendar/events?start=YYYY-MM-DD&end=YYYY-MM-DD
# Auditor or Finance Executive — role-scoped:
#   Auditor: pending reviews (all), finance correction due (all, for
#     visibility/follow-up), exception + anomaly follow-ups, own tasks.
#   Finance: finance correction due for THEIR OWN uploaded documents
#     only (same ownership scoping as GET /reviews/finance-dashboard),
#     own tasks. No pending-review/exception/anomaly data — those stay
#     auditor-only, matching every other endpoint that already
#     restricts them.
# ------------------------------------------------------------
@calendar_bp.route('/events', methods=['GET'])
@jwt_required()
def get_calendar_events():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] not in ('auditor', 'finance_executive'):
        return jsonify({'error': 'Access denied.'}), 403

    start = request.args.get('start')
    end   = request.args.get('end')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        events = []
        if user['role'] == 'auditor':
            events += _pending_review_events(cursor)
            events += _finance_correction_events(cursor)
            events += _exception_events(cursor)
            events += _anomaly_events(cursor)
        else:
            events += _finance_correction_events(cursor, owner_user_id=user['user_id'])
        events += _manual_task_events(cursor, user['user_id'])

        conn.close()

        if start or end:
            events = [e for e in events if _in_range(e['date'], start, end)]

        events.sort(key=lambda e: e['date'] or '')

        return jsonify({'total': len(events), 'events': events}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# ASSIGNABLE USERS (for the Manual Task "assigned user" dropdown) —
# deliberately NOT the admin-only GET /admin/users; scoped to just
# {user_id, full_name, role} for auditor/finance_executive accounts.
# GET /calendar/assignable-users
# ------------------------------------------------------------
@calendar_bp.route('/assignable-users', methods=['GET'])
@jwt_required()
def get_assignable_users():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] not in ('auditor', 'finance_executive'):
        return jsonify({'error': 'Access denied.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            '''SELECT user_id, full_name, role FROM users
               WHERE role IN ('auditor', 'finance_executive') AND is_active = TRUE
               ORDER BY full_name ASC'''
        )
        users = cursor.fetchall()
        conn.close()

        return jsonify({'users': [dict(u) for u in users]}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# CREATE MANUAL AUDIT TASK
# POST /calendar/tasks
# Auditor or Finance Executive
# ------------------------------------------------------------
@calendar_bp.route('/tasks', methods=['POST'])
@jwt_required()
def create_calendar_task():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] not in ('auditor', 'finance_executive'):
        return jsonify({'error': 'Access denied.'}), 403

    data = request.get_json() or {}
    errors, cleaned = validate_task_payload(data)
    if errors:
        return jsonify({'error': '; '.join(errors)}), 400

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        assigned_to = cleaned['assigned_to'] or user['user_id']
        cursor.execute(
            '''INSERT INTO calendar_tasks
               (title, description, event_date, assigned_to, priority, created_by)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING task_id''',
            (cleaned['title'], cleaned['description'], cleaned['event_date'],
             assigned_to, cleaned['priority'], user['user_id'])
        )
        task_id = cursor.fetchone()[0]
        conn.commit()
        conn.close()

        log_audit(user['user_id'], 'CREATE_CALENDAR_TASK', 'calendar_tasks', task_id,
                  f'Created audit task "{cleaned["title"]}" for {cleaned["event_date"]}')

        return jsonify({'message': 'Task created', 'task_id': task_id}), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# MARK MANUAL AUDIT TASK COMPLETE
# PATCH /calendar/tasks/<task_id>/complete
# Only the assignee or the creator may complete their own task.
# ------------------------------------------------------------
@calendar_bp.route('/tasks/<int:task_id>/complete', methods=['PATCH'])
@jwt_required()
def complete_calendar_task(task_id):
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if user['role'] not in ('auditor', 'finance_executive'):
        return jsonify({'error': 'Access denied.'}), 403

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT assigned_to, created_by FROM calendar_tasks WHERE task_id = %s',
            (task_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Task not found'}), 404

        assigned_to, created_by = row
        if user['user_id'] not in (assigned_to, created_by):
            conn.close()
            return jsonify({'error': 'You can only complete a task assigned to or created by you.'}), 403

        cursor.execute(
            "UPDATE calendar_tasks SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE task_id = %s",
            (task_id,)
        )
        conn.commit()
        conn.close()

        return jsonify({'message': 'Task marked complete', 'task_id': task_id, 'status': 'done'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500
