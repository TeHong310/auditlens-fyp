import threading
import psycopg2
import psycopg2.extras
import psycopg2.pool
from config import Config

# ------------------------------------------------------------
# Connection pooling (Phase 1 performance fix). Every route/helper in
# this codebase calls get_db_connection() and later conn.close() —
# hundreds of call sites, none of which this change touches. Instead of
# rewriting all of them to call a pool's own putconn(), _PooledConnection
# overrides ONLY close() to return the connection to the pool instead of
# actually closing the socket; every other method (cursor(), commit(),
# rollback(), etc.) is inherited unchanged from the real psycopg2
# connection class, so existing code keeps behaving exactly as before —
# it just stops paying a fresh TCP/TLS/auth handshake on every call.
#
# The pool itself is created lazily (on first get_db_connection() call,
# not at import time) so merely importing this module — e.g. from a test
# that monkey-patches get_db_connection() entirely and never calls the
# real one — never touches the network, matching the previous behavior.
# ------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()

# Conservative default: this app runs as 2 Gunicorn worker PROCESSES
# (see Dockerfile), each importing this module independently and so
# getting its own pool — total worst case is num_workers * maxconn
# physical connections, well within a small Postgres instance's limit.
# Each worker process currently handles one request at a time (no
# --threads configured), so this ceiling is headroom, not a requirement.
_MIN_POOL_CONNECTIONS = 1
_MAX_POOL_CONNECTIONS = 10


class _PooledConnection(psycopg2.extensions.connection):
    """A psycopg2 connection whose close() returns it to the pool
    instead of tearing down the socket. See module docstring above for
    why this is the mechanism, rather than changing call sites."""

    def close(self):
        # The real psycopg2 close() is safe to call more than once (a
        # no-op after the first) — preserve that exact idempotence so a
        # route that happens to close() twice doesn't newly break.
        if getattr(self, '_returned_to_pool', False):
            return
        self._returned_to_pool = True

        try:
            if not self.closed:
                # Defensive: return the connection to the pool in a
                # clean state. A route that raised partway through a
                # transaction (before reaching its own conn.close())
                # would otherwise leave an open transaction that leaks
                # into whichever request borrows this same connection
                # next. rollback() is always safe to call, including
                # when there is nothing to roll back (e.g. the caller
                # already committed).
                self.rollback()
        except Exception:
            pass

        try:
            _pool.putconn(self)
        except Exception:
            # Never let a pool bookkeeping issue surface as a NEW error
            # to existing callers, who only ever expected close() to
            # succeed silently. Worst case here is this one connection
            # is dropped from the pool's tracking rather than reused —
            # not a correctness issue for the request that already got
            # its data.
            pass


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # re-check inside the lock
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    _MIN_POOL_CONNECTIONS,
                    _MAX_POOL_CONNECTIONS,
                    dbname=Config.DB_NAME,
                    user=Config.DB_USER,
                    password=Config.DB_PASSWORD,
                    host=Config.DB_HOST,
                    port=Config.DB_PORT,
                    connection_factory=_PooledConnection,
                    # Every timestamp column in this schema is `timestamp
                    # without time zone` (naive) and gets its value from
                    # server-side now()/CURRENT_TIMESTAMP defaults, so
                    # whatever the SESSION's timezone happens to be is
                    # silently baked into the stored digits. Without this,
                    # that's whatever the Postgres server's OS/instance
                    # default is - Asia/Kuala_Lumpur on a local dev machine,
                    # but UTC on Render's managed Postgres - so the exact
                    # same code stores different wall-clock digits in
                    # different environments. Forcing UTC here makes every
                    # environment store the same, unambiguous UTC digits,
                    # which the app.py JSON provider then marks explicitly
                    # with a 'Z' suffix on the way out.
                    options='-c timezone=UTC',
                )
    return _pool


def get_db_connection():
    conn = _get_pool().getconn()
    # A connection handed back out of the pool must start each borrow
    # as freshly-idempotent-closeable as a brand-new connection was
    # under the old implementation (its own close() may already have
    # run once, the last time it was returned to the pool).
    conn._returned_to_pool = False
    return conn

def get_user_by_id(user_id):
    conn   = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user