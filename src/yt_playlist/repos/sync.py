"""Library sync rows, attempt admission and guarded publication."""
from yt_playlist.core import sync_state
from yt_playlist.core.published_documents import LIBRARY_SYNC
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos import collection_file_writes


class SyncRepo(Repo):
    @synchronized
    def reserve_file(self, integrity):
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, 'library-sync', 1, integrity)

    @synchronized
    def has_attempt_state(self):
        return sync_state.available(self.conn)

    @synchronized
    def state_facts(self):
        if not sync_state.available(self.conn):
            revision = LIBRARY_SYNC.selected(self.conn)
            if revision is not None:
                return {'document': LIBRARY_SYNC.plan(self.conn, revision)}
            row = self.conn.execute('SELECT value FROM settings WHERE key=?', (sync_state.KEY,)).fetchone()
            return {'raw': row[0] if row and row[0] else '{}'}
        attempt = self.conn.execute('SELECT * FROM sync_attempts WHERE id=1').fetchone()
        cursors = [dict(row) for row in self.conn.execute(
            'SELECT c.*,s.provider_playlist_id FROM sync_cursors c '
            'LEFT JOIN playlist_scopes s ON s.id=c.playlist_scope_id ORDER BY c.id')]
        return {'attempt': dict(attempt) if attempt else None, 'cursors': cursors}

    @synchronized
    def begin_attempt(self, now):
        with self.conn.atomic():
            return sync_state.begin(self.conn, now)

    @synchronized
    def ensure_attempt(self, now):
        with self.conn.atomic():
            self.conn.execute(
                "INSERT INTO sync_attempts(id,revision,phase,started_at) VALUES (1,1,'running',?) "
                "ON CONFLICT(id) DO NOTHING", (now,))

    @synchronized
    def require_revision(self, expected_revision):
        if expected_revision is not None and sync_state.available(self.conn):
            row = self.conn.execute('SELECT revision FROM sync_attempts WHERE id=1').fetchone()
            if row is None or row[0] != expected_revision:
                raise ValueError('Sync attempt was superseded; retained progress was not overwritten')

    @synchronized
    def finish_attempt(self, now, result, *, reference, file_id, expected_revision, last_success, checkpoint=False):
        with self.conn.atomic():
            self.require_revision(expected_revision)
            collection_file_writes.complete(self.conn, file_id, 'library-sync', 1, reference)
            if checkpoint:
                sync_state.write_checkpoint(self.conn, result, reference)
            else:
                sync_state.write_prepared(self.conn, result, now, reference=reference, last_success=last_success)
