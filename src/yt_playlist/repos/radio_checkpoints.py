"""Radio journal storage, revision admission and supported checkpoint cutover."""
import json

from yt_playlist.core import radio_checkpoint, radio_storage
from yt_playlist.repos.base import Repo, lookup_read, synchronized

SESSION_KEY = 'radio_current_session'


class RadioCheckpointRepo(Repo):
    @synchronized
    def remember_playlists(self, playlists, account=None):
        from yt_playlist.core import radio_provenance
        with self.conn:
            for playlist in playlists:
                radio_provenance.remember(self.conn, playlist, account)

    @lookup_read
    def load(self):
        """Return detached state and its compare-and-save token."""
        if radio_storage.available(self.conn):
            return radio_storage.load(self.conn)
        raw = self._store.settings.get_setting(SESSION_KEY)
        return (json.loads(raw) if raw else None), None

    @synchronized
    def save(self, encoded, *, expected, predecessor=None):
        """Reject stale edits; adopt only the exact untouched predecessor import."""
        with self.conn:
            if not radio_storage.available(self.conn):
                self._store.settings.set_setting(SESSION_KEY, encoded)
                return None
            if expected is None and predecessor is not None:
                self.conn.execute('UPDATE radio_session_head SET session_id=session_id WHERE 0')
                imported, revision = radio_storage.load_published(self.conn)
                if (revision == (predecessor.get('episode_id'), 1)
                        and imported is not None
                        and radio_checkpoint.normalize(imported) == radio_checkpoint.normalize(predecessor)):
                    expected = revision
            return radio_storage.save(self.conn, json.loads(encoded), expected=expected)
