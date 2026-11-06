"""Library sync state, attempt admission and atomic compare-and-save."""
import json

from yt_playlist.core import sync_state
from yt_playlist.repos.base import Repo, lookup_read, synchronized


class SyncRepo(Repo):
    @lookup_read
    def state(self):
        return sync_state.read(self.conn)

    @synchronized
    def begin_attempt(self, now):
        with self.conn:
            if sync_state.available(self.conn):
                return sync_state.begin(self.conn, now)
            self._store.settings.set_setting('last_sync_attempt_at', str(now))

    @synchronized
    def finish_attempt(self, now, *, expected_revision, calculate):
        """Calculate against the admitted previous result and publish it atomically."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            typed = sync_state.available(self.conn)
            if typed:
                row = self.conn.execute('SELECT revision FROM sync_attempts WHERE id=1').fetchone()
                if expected_revision is not None and (row is None or row[0] != expected_revision):
                    raise ValueError('Sync attempt was superseded; retained progress was not overwritten')
            result = calculate(sync_state.read(self.conn))
            if typed:
                sync_state.write(self.conn, result, now,
                                 last_success=now if result['status'] == 'complete' else None)
            else:
                values = {'library_sync_state': json.dumps(result), 'last_sync_outcome': result['status']}
                if result['status'] == 'complete':
                    values['last_sync_at'] = str(now)
                self._store.settings.publish_values(values)
            return result
