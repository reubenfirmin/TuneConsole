"""Apply one captured observation atomically with its exactly-once receipt."""
from contextlib import contextmanager

from yt_playlist.core.playback_inbox import inbox
from yt_playlist.repos.base import Repo


class PlaybackFeedbackRepo(Repo):
    def capture(self, kind, identity, body, now):
        return inbox(self._store).capture(kind, identity, body, now)

    def pending(self):
        return inbox(self._store).pending()

    def queued_rating(self, identity, video):
        return inbox(self._store).rating(identity, video)

    def next_observation(self):
        return inbox(self._store).next()

    def acknowledge_observation(self, capture):
        inbox(self._store).acknowledge(capture)

    @contextmanager
    def replaying(self):
        """Admit one local replay without waiting for another replay or writer."""
        queue = inbox(self._store)
        admitted = not self._store.activity_paused and queue.replaying.acquire(blocking=False)
        try:
            yield admitted
        finally:
            if admitted:
                queue.replaying.release()

    def apply_observation(self, row, project):
        """Defer immediately while the writer is busy; never acknowledge a partial replay."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._store.activity_paused:
                return False
            with self.conn.atomic():
                receipt = self.conn.execute(
                    'SELECT 1 FROM playback_feedback_receipts WHERE capture_id=?',
                    (row['capture_id'],)).fetchone()
                if receipt is None:
                    result = project(self._store, row)
                    self.conn.execute('INSERT INTO playback_feedback_receipts VALUES (?,?,?,?,?)',
                        (row['capture_id'], row['account_id'], row['kind'], row['observed_at'], bool(result)))
            return True
        finally:
            self._lock.release()
