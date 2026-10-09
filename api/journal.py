"""
Journal: every session's settings and every action that changed it, in SQLite.

A Session is fully deterministic: the same settings and the same actions in the same order
always give the same session, to the last bit (tested). So instead of saving a session's
state (positions, cash, history, C++ objects, ...), we save what was DONE to it, and rebuild
it by replaying those actions on a fresh Session.

    sessions    one row per session: id, name, settings (JSON), created, last_activity
    actions     one row per change: session id, seq (1, 2, 3, ...), kind, payload (JSON), at

Only actions that succeeded are written, so a refused order is never replayed.
Read-only requests (state, quote, history, recommend) are not written at all.

Replay uses the CURRENT code. If the code changes between saving and restoring, a restored
session is what the new code makes of the same actions.
"""
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    name           TEXT,
    settings       TEXT NOT NULL,
    created        TEXT NOT NULL,
    last_activity  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    session_id     TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    at             TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""


class Journal:
    def __init__(self, path):
        # The file is opened on first use, not here, so merely importing the app (as the
        # tests do) never creates a database file.
        self.path = path
        self.conn = None
        self.lock = threading.Lock()      # one thread uses the connection at a time

    def db(self):
        """The connection, opened (and the tables created) the first time. Call with self.lock held."""
        if self.conn is None:
            # One connection shared by the server's threads (guarded by self.lock)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            # WAL: a crash mid-write can't corrupt the file, and writes are quick
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        return self.conn

    def write(self, statements):
        """
        Runs [(sql, params), ...] as ONE transaction: all of them are saved, or none.
        Call with self.lock held. If any statement fails, everything is rolled back, so a
        half-written save can never be committed later by someone else's write.
        """
        conn = self.db()
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    def add_session(self, session_id, name, settings, created):
        with self.lock:
            self.write([
                # a new id never inherits actions left over under the same id
                ("DELETE FROM actions WHERE session_id = ?", (session_id,)),
                ("INSERT INTO sessions (session_id, name, settings, created, last_activity) VALUES (?, ?, ?, ?, ?)",
                 (session_id, name, json.dumps(settings), created, created)),
            ])

    def add_action(self, session_id, kind, payload, at):
        """Appends one action; returns its sequence number (1 for the first)."""
        with self.lock:
            row = self.db().execute("SELECT COALESCE(MAX(seq), 0) FROM actions WHERE session_id = ?",
                                    (session_id,)).fetchone()
            seq = row[0] + 1
            self.write([
                ("INSERT INTO actions (session_id, seq, kind, payload, at) VALUES (?, ?, ?, ?, ?)",
                 (session_id, seq, kind, json.dumps(payload), at)),
                ("UPDATE sessions SET last_activity = ? WHERE session_id = ?", (at, session_id)),
            ])
            return seq

    def session_row(self, session_id):
        """The session's stored settings, or None if there is no such session."""
        with self.lock:
            row = self.db().execute(
                "SELECT session_id, name, settings, created, last_activity FROM sessions WHERE session_id = ?",
                (session_id,)).fetchone()
        if row is None:
            return None
        return {"session_id": row[0], "name": row[1], "settings": json.loads(row[2]),
                "created": row[3], "last_activity": row[4]}

    def actions(self, session_id):
        """Every action of a session, in the order they happened: [(kind, payload), ...]."""
        with self.lock:
            rows = self.db().execute("SELECT kind, payload FROM actions WHERE session_id = ? ORDER BY seq",
                                     (session_id,)).fetchall()
        result = []
        for kind, payload in rows:
            result.append((kind, json.loads(payload)))
        return result

    def all_sessions(self):
        """Every stored session with its action count, newest activity first."""
        with self.lock:
            rows = self.db().execute(
                "SELECT s.session_id, s.name, s.settings, s.created, s.last_activity, COUNT(a.seq) "
                "FROM sessions s LEFT JOIN actions a ON a.session_id = s.session_id "
                "GROUP BY s.session_id ORDER BY s.last_activity DESC").fetchall()
        result = []
        for row in rows:
            result.append({"session_id": row[0], "name": row[1], "settings": json.loads(row[2]),
                           "created": row[3], "last_activity": row[4], "actions": row[5]})
        return result

    def count(self):
        with self.lock:
            return self.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def delete(self, session_id):
        with self.lock:
            self.write([
                ("DELETE FROM actions WHERE session_id = ?", (session_id,)),
                ("DELETE FROM sessions WHERE session_id = ?", (session_id,)),
            ])