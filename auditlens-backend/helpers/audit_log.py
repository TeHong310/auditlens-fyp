from db import get_db_connection

def log_audit(user_id, action, target_table=None, target_id=None, description=None):
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO audit_logs (user_id, action, target_table, target_id, description)
               VALUES (%s, %s, %s, %s, %s)''',
            (user_id, action, target_table, target_id, description)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Audit log error: {e}")