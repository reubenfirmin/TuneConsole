"""SQL references and guarded replacement for collection recovery."""
from contextlib import contextmanager

from yt_playlist.core.collection_owners import OWNERS, OWNER_KEYS
from yt_playlist.core.policy_documents import coordinate_updates
from yt_playlist.repos.connection import ConnectionRepo


def references(conn):
    for table, kind in OWNERS.items():
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if 'recovery_filename' in columns:
            key = OWNER_KEYS.get(table, 'id')
            for identity, filename, digest, size in conn.execute(
                    f'SELECT {key},recovery_filename,recovery_digest,recovery_bytes FROM {table} '
                    f'WHERE recovery_filename IS NOT NULL ORDER BY {key}'):
                yield table, identity, kind, filename, digest, size


class CollectionRecovery(ConnectionRepo):
    @property
    def pinned(self):
        with self._lock:
            return self.conn.in_transaction

    def references(self):
        with self._lock:
            yield from references(self.conn)

    @contextmanager
    def restoring(self):
        with self._lock:
            if self.conn.in_transaction:
                raise ValueError('Collection restore requires an idle connection')
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def identity(self, identity):
        with self._lock:
            return self.conn.execute('SELECT kind,owner_id,content_digest,byte_size,file_status '
                'FROM collection_file_writes WHERE id=?', (identity,)).fetchone()

    def replace(self, files):
        with self._lock, coordinate_updates(self.conn, (table for table, _ in files)):
            for (table, identity), filename in files.items():
                if table not in OWNERS:
                    raise ValueError('Unknown collection recovery owner')
                key = OWNER_KEYS.get(table, 'id')
                self.conn.execute(f'UPDATE {table} SET recovery_filename=? WHERE {key}=?', (filename, identity))
