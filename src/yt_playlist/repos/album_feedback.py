"""Release-response rows and their durable response intents."""
from contextlib import contextmanager

from yt_playlist.core import album_feedback_outbox, album_feedback_storage, albums
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.core.db_writes import atomic_write, reserved_write
from yt_playlist.core.published_documents import ALBUM_FEEDBACK
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read


class AlbumFeedbackRepo(Repo):
    @contextmanager
    def transaction(self):
        with atomic_write(self._store):
            # Deferred admission can precede the bounded outbox migration.
            album_feedback_outbox.install(self.conn)
            yield

    @lookup_read
    def uses_rows(self):
        return album_feedback_storage.available(self.conn)

    @snapshot_read
    def original_facts(self):
        with setting_reads(self._store, ('album_feedback',)):
            if album_feedback_storage.available(self.conn):
                return {'rows': [dict(row) for row in album_feedback_storage.rows(self.conn)]}
            row = self.conn.execute("SELECT value FROM settings WHERE key='album_feedback'").fetchone()
            revision = ALBUM_FEEDBACK.selected(self.conn)
            return {'raw': row[0] if row else None, 'selected': revision is not None,
                    'plan': ALBUM_FEEDBACK.plan(self.conn, revision) if revision is not None else None}

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
            self.conn.execute('UPDATE album_feedback SET withdrawn_at=? WHERE album_id=? AND withdrawn_at IS NULL '
                              'AND account_id=?', (now, album_id, account_id))
            self.conn.execute('INSERT INTO album_feedback_legacy_dismissals VALUES (?,?,?) '
                'ON CONFLICT(account_id,album_id) DO UPDATE SET dismissed_at=excluded.dismissed_at',
                (account_id, album_id, now))
            if action is not None:
                self.conn.execute('INSERT INTO album_feedback(account_id,album_id,source_browse_id,response,created_at,'
                    'opportunity_id,action_id) VALUES (?,?,?,?,?,?,?)',
                    (account_id, album_id, browse, kind, now, opportunity_id, action))
            self._save_intents(intents)

    def save_legacy(self, original_raw, updated_raw, intents):
        with atomic_write(self._store):
            row = self.conn.execute("SELECT value FROM settings WHERE key='album_feedback'").fetchone()
            if (row[0] if row else None) != original_raw:
                return False
            album_feedback_outbox.install(self.conn)
            self.conn.execute("INSERT INTO settings(key,value) VALUES ('album_feedback',?) ON CONFLICT(key) "
                'DO UPDATE SET value=excluded.value WHERE value IS NOT excluded.value', (updated_raw,))
            self._save_intents(intents)
            return True

    def _save_intents(self, intents):
        for intent in intents:
            album_feedback_outbox.enqueue(self.conn, **intent)

    def retained_opportunities(self):
        with reserved_write(self._store, wait_seconds=0):
            return ({row[0] for row in self.conn.execute(
                'SELECT DISTINCT opportunity_id FROM '
                f'{album_feedback_outbox.TABLE} WHERE delivered=0')}
                if album_feedback_outbox.available(self.conn) else set())

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
