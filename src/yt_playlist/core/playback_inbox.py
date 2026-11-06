"""Durable local feedback captured independently of maintenance's SQLite writer.

The small per-database inbox contains confirmed remote results and raw playback
observations. Replay performs local writes only. A main-database receipt commits
with those writes, so a crash before removing the inbox row cannot repeat them.
"""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

from yt_playlist.core import fao, policy_documents

_INBOX = """CREATE TABLE IF NOT EXISTS playback_feedback_inbox (
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
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576)
)"""


class Inbox:
    def __init__(self, database, memory_key):
        self.path = (database.with_name(database.name + '.playback-feedback.sqlite3') if database else
                     Path(tempfile.gettempdir()) / ('tuneconsole-playback-' + memory_key + '.sqlite3'))
        self.lock = threading.Lock()
        self.replaying = threading.Lock()
        self.directory = self.path.with_name(self.path.name + '.observations')

    @contextmanager
    def admission(self):
        """Coordinate captures/acks with a short cross-process backup snapshot.

        Once a remote result has been confirmed, shutdown cancellation must not
        discard its local capture. Admission waits are bounded independently of
        the cancellable background conversion.
        """
        with self.lock, self.path.with_name(self.path.name + '.lock').open('a+b') as fence:
            deadline = time.monotonic() + 5
            while True:
                unlock = policy_documents._try_file_lock(fence, exclusive=True)
                if unlock is not None:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Playback feedback is busy; retry the capture')
                time.sleep(.025)
            try:
                yield
            finally:
                unlock()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA synchronous=FULL')
            if 'body' in {row[1] for row in conn.execute('PRAGMA table_info(playback_feedback_inbox)')}:
                self._relocate_legacy(conn)
            conn.execute(_INBOX)
            conn.execute('CREATE INDEX IF NOT EXISTS ix_playback_feedback_rating '
                         'ON playback_feedback_inbox(kind,account_id,video_id,sequence)')
            conn.execute('CREATE INDEX IF NOT EXISTS ix_playback_feedback_filename '
                         'ON playback_feedback_inbox(filename)')
            return conn
        except BaseException:
            conn.close()
            raise

    def _publish(self, capture, kind, body):
        encoded = json.dumps({'format': 1, 'capture_id': capture, 'kind': kind, 'body': body},
                             allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
        if not 0 < len(encoded) <= 1048576:
            raise ValueError('Playback observation exceeds its storage budget')
        digest = hashlib.sha256(encoded).hexdigest()
        filename = policy_documents._publish_encoded(self.directory, encoded, digest)
        return str(filename), digest, len(encoded)

    def _relocate_legacy(self, conn):
        """Retain captures made by the earlier development inbox transactionally."""
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(_INBOX.replace('IF NOT EXISTS playback_feedback_inbox', 'playback_feedback_inbox_v2'))
            for row in conn.execute('SELECT * FROM playback_feedback_inbox ORDER BY sequence').fetchall():
                owner = self._publish(row['capture_id'], row['kind'], json.loads(row['body']))
                conn.execute('INSERT INTO playback_feedback_inbox_v2 VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (row['sequence'], row['capture_id'], row['kind'], row['account_id'], row['observed_at'],
                     row['video_id'], row['rating_status'], *owner))
            conn.execute('DROP TABLE playback_feedback_inbox')
            conn.execute('ALTER TABLE playback_feedback_inbox_v2 RENAME TO playback_feedback_inbox')

    def capture(self, kind, identity, body, now):
        capture = uuid.uuid4().hex
        video = body.get('video_id') if kind == 'rating' else body.get('videoId')
        status = body.get('status') if kind == 'rating' else None
        # A collector cannot visit file-before-SQL publication, even when
        # backup admission is held or publication subsequently rolls back.
        with fao.retained_reader(self.directory):
            owner = self._publish(capture, kind, body)
            with self.admission(), closing(self.connect()) as conn, conn:
                conn.execute('INSERT INTO playback_feedback_inbox(capture_id,kind,account_id,observed_at,video_id,rating_status,'
                             'filename,content_digest,byte_size) VALUES (?,?,?,?,?,?,?,?,?)',
                             (capture, kind, identity, now, video, status, *owner))
        return capture

    def pending(self):
        if not self.path.exists():
            return 0
        with self.admission(), closing(self.connect()) as conn:
            return conn.execute('SELECT count(*) FROM playback_feedback_inbox').fetchone()[0]

    def next(self):
        if not self.path.exists():
            return None
        with self.admission(), closing(self.connect()) as conn:
            row = conn.execute('SELECT * FROM playback_feedback_inbox ORDER BY sequence LIMIT 1').fetchone()
            return dict(row) if row else None

    def rating(self, identity, video):
        if not self.path.exists():
            return None
        with self.admission(), closing(self.connect()) as conn:
            row = conn.execute("SELECT observed_at,rating_status AS status "
                "FROM playback_feedback_inbox WHERE kind='rating' AND account_id=? "
                "AND video_id=? ORDER BY sequence DESC LIMIT 1", (identity, video)).fetchone()
            return {'status': row['status'], 'observed_at': row['observed_at'], 'source': 'api'} if row else None

    def acknowledge(self, capture):
        with self.admission(), closing(self.connect()) as conn:
            with conn:
                row = conn.execute('SELECT filename FROM playback_feedback_inbox WHERE capture_id=?',
                                   (capture,)).fetchone()
                conn.execute('DELETE FROM playback_feedback_inbox WHERE capture_id=?', (capture,))
            if row is not None:
                from . import fao
                fao.delete(row['filename'], missing_ok=True)

    @staticmethod
    def body(row):
        path = Path(row['filename'])
        from . import fao
        encoded = fao.read_bytes(path, max_bytes=row['byte_size'])
        if (len(encoded) != row['byte_size'] or path.stem != row['content_digest']
                or hashlib.sha256(encoded).hexdigest() != row['content_digest']):
            raise ValueError('Playback observation does not match its retained content')
        document = json.loads(encoded)
        if (set(document) != {'format', 'capture_id', 'kind', 'body'} or document['format'] != 1
                or document['capture_id'] != row['capture_id'] or document['kind'] != row['kind']
                or not isinstance(document['body'], dict)):
            raise ValueError('Invalid retained playback observation')
        return document['body']


def inbox(store):
    return getattr(store, '_evidence_source', store)._playback_feedback


def deferred(store):
    return bool(store.activity_paused or store.playback_feedback.pending())


def capture(store, kind, identity, body, now):
    return store.playback_feedback.capture(kind, identity, body, now)


def _queue(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return Inbox(Path(path).resolve() if path else None, getattr(conn, '_policy_memory_key', str(id(conn))))


def directory(conn):
    return _queue(conn).directory


def owns_file(conn, filename):
    """Pending captures remain required even if main already has a receipt.

    The collector holds the observation root exclusively, excluding capture.
    Backup's admission fence prevents acknowledgements while it copies pending
    rows, so a current row protects both replay and coherent backup custody.
    A busy or predecessor inbox is retained and retried without waiting.
    """
    queue = _queue(conn)
    if not queue.path.exists():
        return False
    try:
        with closing(sqlite3.connect(queue.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0)) as reader:
            columns = {row[1] for row in reader.execute('PRAGMA table_info(playback_feedback_inbox)')}
            if 'filename' not in columns:
                return True
            return reader.execute('SELECT 1 FROM playback_feedback_inbox WHERE filename=? LIMIT 1',
                                  (str(filename),)).fetchone() is not None
    except sqlite3.OperationalError as exc:
        if 'locked' in str(exc) or 'busy' in str(exc):
            return True
        raise
