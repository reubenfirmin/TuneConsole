"""Installed workflow source and target facts; no files or remote operations."""
from yt_playlist.core import playlist_scopes
from yt_playlist.repos.connection import ConnectionRepo


class WorkflowSources(ConnectionRepo):
    def __init__(self, conn):
        super().__init__(conn)
        self._columns = {}

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

    @staticmethod
    def table(name, suffix):
        if name not in {'gc', 'playlists', 'albums'} or suffix not in {'parts', 'attempts', 'receipts', 'items'}:
            raise ValueError('Unknown workflow preparation owner')
        return 'prepared_workflow_' + name + '_' + suffix

    def parts(self, name, header):
        with self._lock:
            return self.conn.execute('SELECT * FROM ' + self.table(name, 'parts')
                + ' WHERE source=? AND preparation_revision=? ORDER BY position',
                (header['source'], header['preparation_revision'])).fetchall()

    def member(self, name, suffix, source, revision, values):
        with self._lock:
            table = self.table(name, suffix)
            if table not in self._columns:
                self._columns[table] = self.conn.execute('PRAGMA table_info(' + table + ')').fetchall()
            columns = self._columns[table]
            keys = [(column[1], value) for column, value in zip(columns[2:], values) if column[5]]
            row = self.conn.execute('SELECT * FROM ' + table + ' WHERE source=? AND preparation_revision=?'
                + ''.join(' AND ' + key + ' IS ?' for key, _ in keys),
                (source, revision, *(value for _, value in keys))).fetchone()
            return tuple(row)[2:] if row is not None else None

    def count(self, name, suffix, source, revision):
        with self._lock:
            return self.conn.execute('SELECT count(*) FROM ' + self.table(name, suffix)
                + ' WHERE source=? AND preparation_revision=?', (source, revision)).fetchone()[0]
