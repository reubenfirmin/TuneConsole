"""Library sync rows, attempt admission and guarded publication."""
from yt_playlist.core import sync_state
from yt_playlist.repos.base import Repo, snapshot_read, synchronized
from yt_playlist.repos import collection_file_writes


class SyncRepo(Repo):
    @synchronized
    def reserve_file(self, integrity):
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, 'library-sync', 1, integrity)

    @snapshot_read
    def state_facts(self):
        attempt = sync_state.head(self.conn)
        cursors = [dict(row) for row in sync_state.cursors(self.conn)]
        return {'attempt': dict(attempt) if attempt else None, 'cursors': cursors}

    @synchronized
    def begin_attempt(self, now):
        with self.conn.atomic():
            return sync_state.begin(self.conn, now)

    @synchronized
    def ensure_attempt(self, now):
        with self.conn.atomic():
            sync_state.ensure(self.conn, now)

    @synchronized
    def require_revision(self, expected_revision):
        if expected_revision is not None:
            attempt = sync_state.head(self.conn)
            if attempt is None or attempt['revision'] != expected_revision:
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
