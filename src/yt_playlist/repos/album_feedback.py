"""Release-response rows and their durable response intents."""
from contextlib import contextmanager

from yt_playlist.core import album_feedback_outbox, album_feedback_storage, album_playback_context, albums
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_writes import atomic_write, reserved_write
from yt_playlist.repos.base import Repo, lookup_read, synchronized


class AlbumFeedbackRepo(Repo):
    @contextmanager
    def transaction(self):
        with atomic_write(self._store):
            yield

    @lookup_read
    def original_rows(self):
        return [dict(row) for row in album_feedback_storage.rows(self.conn)]

    @lookup_read
    def account(self, account_id):
        if account_id is None:
            row = self.conn.execute('SELECT id FROM accounts WHERE active=1 AND is_master=1').fetchone()
            account_id = row[0] if row else None
        active = self.conn.execute('SELECT 1 FROM accounts WHERE id=? AND active=1', (account_id,)).fetchone() is not None
        return account_id, active

    @lookup_read
    def replacement_facts(self, browse, account_id):
        album_id = albums.lookup(self.conn, browse)
        previous = [dict(row) for row in self.conn.execute(
            'SELECT * FROM album_feedback WHERE album_id=? AND account_id=?', (album_id, account_id))]
        return album_id, previous

    def save_response(self, album_id, account_id, browse, kind, now, opportunity_id, action, intents):
        with self._lock:
            if album_id is None:
                album_id = self.conn.execute('INSERT INTO albums(metadata_rank) VALUES (0)').lastrowid
                self.conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id) VALUES ('youtube',?,?)",
                                  (browse, album_id))
            album_feedback_storage.save_response(self.conn, album_id, account_id, browse, kind, now, opportunity_id, action)
            self._save_intents(intents)

    def _save_intents(self, intents):
        for intent in intents:
            album_feedback_outbox.enqueue(self.conn, **intent)

    @lookup_read
    def merge_inputs(self, target, retired):
        return [dict(row) for row in self.conn.execute('SELECT * FROM album_feedback '
            'WHERE album_id IN (?,?) AND withdrawn_at IS NULL', (target, retired))]

    @synchronized
    def apply_merge(self, target, retired, withdrawals):
        self.conn.executemany('UPDATE album_feedback SET withdrawn_at=? WHERE id=?', withdrawals)
        self.conn.execute('UPDATE album_feedback SET album_id=? WHERE album_id=?', (target, retired))
        album_playback_context.move_native(self.conn, retired, target)

    def retained_opportunities(self):
        with reserved_write(self._store, wait_seconds=0):
            return album_feedback_outbox.retained_opportunities(self.conn)

    def pending_responses(self, *, after, limit):
        """Detach a bounded prefix only when the local connection is idle."""
        if not self._lock.acquire(blocking=False):
            return []
        try:
            check_cancellable_work()
            if self.conn.in_transaction:
                return []
            return [dict(row) for row in album_feedback_outbox.pending(self.conn, after=after, limit=limit)]
        finally:
            self._lock.release()

    def acknowledge_response(self, action_id):
        with atomic_write(self._store, wait_seconds=0):
            album_feedback_outbox.acknowledge(self.conn, action_id)
