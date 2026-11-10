"""Persist receipts for completed playback observations."""
from contextlib import contextmanager

from yt_playlist.repos.base import Repo, synchronized


class PlaybackFeedbackRepo(Repo):
    @contextmanager
    def try_access(self):
        """Defer replay when the serving connection is busy; open no transaction."""
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    @synchronized
    def has_receipt(self, capture_id):
        return self.conn.execute('SELECT 1 FROM playback_feedback_receipts WHERE capture_id=?',
                                 (capture_id,)).fetchone() is not None

    @synchronized
    def record_receipt(self, row, result):
        self.conn.execute('INSERT INTO playback_feedback_receipts VALUES (?,?,?,?,?)',
            (row['capture_id'], row['account_id'], row['kind'], row['observed_at'], result))
        self.conn.commit()
