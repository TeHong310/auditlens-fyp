from flask import Blueprint, jsonify, request
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
import bcrypt
import psycopg2.extras
from db import get_db_connection, get_user_by_id
from helpers.audit_log import log_audit

auth_bp = Blueprint('auth', __name__)

# ------------------------------------------------------------
# REGISTER
# POST /auth/register
# ------------------------------------------------------------
@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()

    required_fields = ['full_name', 'email', 'password', 'role']
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({'error': f'{field} is required'}), 400

    full_name = data['full_name'].strip()
    email     = data['email'].strip().lower()
    password  = data['password']
    role      = data['role'].strip().lower()

    # Admin is deliberately NOT in this list — an admin account must
    # never be creatable through public self-registration. The one
    # initial admin account is seeded directly into the database at
    # startup (see app.py::_ensure_admin_seed_account()); any further
    # admin accounts can only be created by an existing admin via the
    # authenticated POST /admin/users/create endpoint (routes/admin.py),
    # which is intentionally left unchanged and still allows 'admin'
    # there since that action already requires an admin's own JWT.
    allowed_roles = ['finance_executive', 'auditor']
    if role not in allowed_roles:
        return jsonify({'error': f'Invalid role. Must be one of: {allowed_roles}'}), 400

    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT user_id FROM users WHERE email = %s', (email,))
        if cursor.fetchone():
            conn.close()
            return jsonify({'error': 'Email already registered'}), 409

        cursor.execute(
            '''INSERT INTO users (full_name, email, password_hash, role)
               VALUES (%s, %s, %s, %s) RETURNING user_id''',
            (full_name, email, password_hash, role)
        )
        user_id = cursor.fetchone()[0]
        conn.commit()
        conn.close()

        log_audit(user_id, 'REGISTER', 'users', user_id, f'New user registered: {email} as {role}')

        return jsonify({
            'message': 'User registered successfully',
            'user_id': user_id,
            'email':   email,
            'role':    role
        }), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# LOGIN
# POST /auth/login
# ------------------------------------------------------------
@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()

    if not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password are required'}), 400

    email    = data['email'].strip().lower()
    password = data['password']

    try:
        conn   = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute('SELECT * FROM users WHERE email = %s', (email,))
        user = cursor.fetchone()
        conn.close()

        if not user:
            return jsonify({'error': 'Invalid email or password'}), 401

        if not user['is_active']:
            return jsonify({'error': 'Account is deactivated. Contact admin.'}), 403

        if not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            return jsonify({'error': 'Invalid email or password'}), 401

        access_token = create_access_token(identity=str(user['user_id']))

        log_audit(user['user_id'], 'LOGIN', 'users', user['user_id'], f'User logged in: {email}')

        return jsonify({
            'message':      'Login successful',
            'access_token': access_token,
            'user': {
                'user_id':   user['user_id'],
                'full_name': user['full_name'],
                'email':     user['email'],
                'role':      user['role']
            }
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ------------------------------------------------------------
# GET PROFILE
# GET /auth/me
# ------------------------------------------------------------
@auth_bp.route('/me', methods=['GET'])
@jwt_required()
def get_profile():
    user_id = get_jwt_identity()
    user    = get_user_by_id(user_id)

    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify({'user': {
        'user_id':    user['user_id'],
        'full_name':  user['full_name'],
        'email':      user['email'],
        'role':       user['role'],
        'is_active':  user['is_active'],
        'created_at': str(user['created_at'])
    }}), 200