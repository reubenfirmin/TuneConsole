"""Installed workflow source and target facts; no files or remote operations."""
from yt_playlist.core import playlist_scopes
from yt_playlist.repos.connection import ConnectionRepo


class WorkflowSources(ConnectionRepo):
    def targets(self, requests):
        with self._lock:
            return {request: self.target(*request) for request in requests}

    def target(self, account, remote):
        with self._lock:
            known = self.conn.execute('SELECT id FROM main.identities WHERE id=?', (account,)).fetchone() if account is not None else None
            if not known:
                return None, None, 'unrecorded_account' if account is None else 'missing_account'
            if remote is None:
                return known[0], None, None
            scope = playlist_scopes.resolve(self.conn, remote, account)
            observed = self.conn.execute('SELECT ref FROM main.playlist_scopes WHERE ref=?', (scope,)).fetchone()
            return known[0], observed[0] if observed else None, None if observed else 'missing_scope'

    def source(self, key, max_bytes):
        with self._lock:
            return self.conn.execute('SELECT key,CASE WHEN value IS NULL THEN NULL ELSE '
                "COALESCE(substr(CAST(value AS BLOB),1,?),X'') END AS value FROM main.settings WHERE key=?",
                (max_bytes + 1, key)).fetchone()

    @property
    def idle(self):
        with self._lock:
            return not self.conn.in_transaction
