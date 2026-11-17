"""The independent SQLite inbox: reservation, readiness, replay and acknowledgement."""
from contextlib import closing
import sqlite3

SCHEMA = """CREATE TABLE IF NOT EXISTS playback_feedback_inbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('rating','player','live')),
    account_id INTEGER NOT NULL CHECK(typeof(account_id)='integer'),
    observed_at REAL NOT NULL CHECK(typeof(observed_at) IN ('integer','real') AND
                                  observed_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308),
    video_id TEXT,
    rating_status TEXT CHECK(rating_status IS NULL OR rating_status IN ('LIKE','DISLIKE','INDIFFERENT')),
    filename TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),
    file_status TEXT NOT NULL DEFAULT 'ready' CHECK(file_status IN ('pending','ready'))
)"""


class PlaybackInboxRepo:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA synchronous=FULL')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}
            if not columns:
                conn.execute(SCHEMA)
                columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}
            if 'filename' in columns:
                if 'file_status' not in columns:
                    # A CHECK on ADD COLUMN makes SQLite scan all installed rows.
                    # Existing captures adopt ready through the constant default;
                    # reserve/complete supply the two allowed states explicitly.
                    conn.execute("ALTER TABLE playback_feedback_inbox ADD COLUMN file_status "
                                 "TEXT NOT NULL DEFAULT 'ready'")
                conn.execute('CREATE INDEX IF NOT EXISTS ix_playback_feedback_filename '
                             'ON playback_feedback_inbox(filename)')
            conn.execute('CREATE INDEX IF NOT EXISTS ix_playback_feedback_rating '
                         'ON playback_feedback_inbox(kind,account_id,video_id,sequence)')
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _ready(conn):
        columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}
        return "file_status='ready'" if 'file_status' in columns else '1'

    def reserve(self, capture, kind, account, now, video, status, reference, inline):
        """Commit the owning row before the controller starts a file write."""
        with closing(self.connect()) as conn, conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}
            if 'body' in columns:
                conn.execute('INSERT INTO playback_feedback_inbox(capture_id,kind,account_id,observed_at,'
                             'video_id,rating_status,body) VALUES (?,?,?,?,?,?,?)',
                             (capture, kind, account, now, video, status, inline))
                return False
            conn.execute('INSERT INTO playback_feedback_inbox(capture_id,kind,account_id,observed_at,video_id,'
                         "rating_status,filename,content_digest,byte_size,file_status) VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                         (capture, kind, account, now, video, status, *reference))
        return True

    def complete(self, capture):
        with closing(self.connect()) as conn, conn:
            if conn.execute("UPDATE playback_feedback_inbox SET file_status='ready' "
                            "WHERE capture_id=? AND file_status='pending'", (capture,)).rowcount != 1:
                raise ValueError('Playback observation reservation is no longer pending')

    def recovery_page(self, *, after=0, limit=32):
        if not self.path.exists():
            return {'after': 0, 'pending': []}
        with closing(self.connect()) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}
            if 'file_status' not in columns:
                return {'after': 0, 'pending': []}
            # Seek the primary key before filtering. A pending-only query without
            # a new index would scan every ready observation on every replay turn.
            rows = [dict(row) for row in conn.execute('SELECT * FROM playback_feedback_inbox '
                'WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit))]
            return {'after': rows[-1]['sequence'] if len(rows) == limit else 0,
                    'pending': [row for row in rows if row['file_status'] == 'pending']}

    def pending(self):
        if not self.path.exists():
            return 0
        with closing(self.connect()) as conn:
            return conn.execute('SELECT count(*) FROM playback_feedback_inbox WHERE '+self._ready(conn)).fetchone()[0]

    def next(self):
        if not self.path.exists():
            return None
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT * FROM playback_feedback_inbox WHERE '+self._ready(conn)+
                               ' ORDER BY sequence LIMIT 1').fetchone()
            return dict(row) if row else None

    def rating(self, account, video):
        if not self.path.exists():
            return None
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT observed_at,rating_status AS status FROM playback_feedback_inbox "
                "WHERE kind='rating' AND account_id=? AND video_id=? AND "+self._ready(conn)+
                ' ORDER BY sequence DESC LIMIT 1', (account, video)).fetchone()
            return {'status': row['status'], 'observed_at': row['observed_at'], 'source': 'api'} if row else None

    def acknowledge(self, capture):
        with closing(self.connect()) as conn, conn:
            row = conn.execute('SELECT * FROM playback_feedback_inbox WHERE capture_id=? AND '+self._ready(conn),
                               (capture,)).fetchone()
            if row is not None:
                conn.execute('DELETE FROM playback_feedback_inbox WHERE capture_id=?', (capture,))
            return row['filename'] if row is not None and 'filename' in row.keys() else None


def owns_file(path, filename):
    if not path.exists():
        return False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0)) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}
            if 'filename' not in columns:
                return True
            ready = " AND file_status='ready'" if 'file_status' in columns else ''
            return conn.execute('SELECT 1 FROM playback_feedback_inbox WHERE filename=?'+ready+' LIMIT 1',
                                (str(filename),)).fetchone() is not None
    except sqlite3.OperationalError as exc:
        if getattr(exc, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        raise
