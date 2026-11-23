"""Persist receipts for completed playback observations."""
from contextlib import contextmanager

from yt_playlist.core.db_writes import atomic_write
from yt_playlist.repos.base import Repo, synchronized


class PlaybackFeedbackRepo(Repo):
    @contextmanager
    def try_access(self):
        """Defer replay when the serving connection is busy; open no transaction."""
        if not self._lock.acquire(blocking=False):
            yield False
            return
        try:
            # A replay must own the commit before it can acknowledge its file.
            yield not self.conn.in_transaction
        finally:
            self._lock.release()

    def projection(self):
        return atomic_write(self._store)

    @synchronized
    def has_receipt(self, capture_id):
        return self.receipt_result(capture_id) is not None

    @synchronized
    def receipt_result(self, capture_id):
        row = self.conn.execute('SELECT result FROM playback_feedback_receipts WHERE capture_id=?',
                                (capture_id,)).fetchone()
        return bool(row[0]) if row is not None else None

    @synchronized
    def record_receipt(self, row, result):
        self.conn.execute('INSERT INTO playback_feedback_receipts VALUES (?,?,?,?,?)',
            (row['capture_id'], row['account_id'], row['kind'], row['observed_at'], result))
