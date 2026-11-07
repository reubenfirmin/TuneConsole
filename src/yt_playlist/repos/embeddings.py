"""Canonical embedding subjects, heads and prepared SQL publication."""
from yt_playlist.core import artists, embedding_files, embeddings
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.repos.base import Repo, synchronized, lookup_read


class EmbeddingRepo(Repo):
    @synchronized
    def delete_spaces(self, recording_spaces, artist_spaces):
        with self.conn.atomic():
            for table, spaces in (('recording_embeddings', recording_spaces), ('artist_embeddings', artist_spaces)):
                if spaces is None:
                    self.conn.execute(f'DELETE FROM {table}')
                else:
                    self.conn.executemany(f'DELETE FROM {table} WHERE space=?', [(space,) for space in spaces])
            if embedding_files.available(self.conn):
                for table, spaces in (('recording_embedding_files', recording_spaces), ('artist_embedding_files', artist_spaces)):
                    if spaces is None:
                        self.conn.execute(f'DELETE FROM {table}')
                    else:
                        self.conn.executemany(f'DELETE FROM {table} WHERE space=?', [(space,) for space in spaces])

    @lookup_read
    def rows(self, name, keys=None):
        with column_group_reads(self._store, 'recordings', 'identity'):
            return embeddings.row_facts(self.conn, name, keys=keys)

    @lookup_read
    def head(self, name):
        return embeddings.head(self.conn, name)

    @lookup_read
    def count(self, name):
        return embeddings.count(self.conn, name)

    @lookup_read
    def file_owned(self):
        return embedding_files.available(self.conn)

    @lookup_read
    def subjects(self, kind, keys, *, retired=False):
        result = {}
        for key in keys:
            if kind == 'recording':
                current = '' if retired else ' AND NOT EXISTS(SELECT 1 FROM recording_redirects d WHERE d.retired_id=r.id)'
                with column_group_reads(self._store, 'recordings', 'identity'):
                    row = self.conn.execute('SELECT r.id FROM recordings r WHERE r.identity_key=?' + current, (key,)).fetchone()
                result[key] = row[0] if row else None
            elif retired and str(key).startswith('@artist:'):
                result[key] = int(key[8:])
            else:
                subject = artists.lookup(self.conn, key)
                if isinstance(key, int) or str(key).startswith('@artist:'):
                    try:
                        supplied = int(str(key).removeprefix('@artist:'))
                    except ValueError:
                        supplied = None
                    if supplied != subject:
                        subject = None
                result[key] = subject
        return result

    @lookup_read
    def insertion_orders(self, kind, space, subjects, removed):
        logical, column = embedding_files.relation(self.conn, kind), kind + '_id'
        orders = {}
        for start in range(0, len(subjects), 500):
            batch = subjects[start:start + 500]
            orders.update(self.conn.execute(f'SELECT {column},insertion_order FROM {logical} '
                'WHERE space=? AND ' + column + ' IN (' + ','.join('?' for _ in batch) + ')', (space, *batch)))
        removed = [subject for subject in removed if subject is not None]
        excluded = (' AND ' + column + ' NOT IN (' + ','.join('?' for _ in removed) + ')') if removed else ''
        order = self.conn.execute(f'SELECT COALESCE(MAX(insertion_order),0) FROM {logical} '
                                  'WHERE space=?' + excluded, (space, *removed)).fetchone()[0]
        return orders, order

    @synchronized
    def publish(self, publications):
        with self.conn.atomic():
            for publication in publications:
                embeddings.publish_prepared(self.conn, publication)

    @lookup_read
    def assert_reader(self):
        if not getattr(self._store, '_read_only', False):
            raise RuntimeError('Training changes require a read snapshot')
