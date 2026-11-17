"""Resumable SQL ownership checks for a bounded collection-file batch."""
from contextlib import contextmanager

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.collection_owners import OWNERS
from .base import Repo, synchronized


class OwnershipScan:
    def __init__(self):
        self.reset()

    def reset(self):
        self._epoch = None
        self._tables = ()
        self._table_index = 0
        self._after = None
        self._owned = set()

    def _ownership_epoch(self, conn):
        schema = conn.execute('PRAGMA main.schema_version').fetchone()[0]
        if (conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='collection_recovery_epoch'").fetchone()
                and conn.execute("SELECT 1 FROM main.schema_changes WHERE name='collection_recovery_scan_keys'").fetchone()):
            revision = conn.execute('SELECT revision FROM main.collection_recovery_epoch WHERE id=1').fetchone()
            if revision is None:
                raise ValueError('Missing collection recovery ownership revision')
            return id(conn), schema, revision[0]
        tables = (*OWNERS, 'action_backups')
        external = conn.execute('PRAGMA main.data_version').fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_temp_schema WHERE name='snapshot_revisions'").fetchone():
            marks = ','.join('?' for _ in tables)
            revisions = tuple(tuple(row) for row in conn.execute(
                'SELECT table_name,revision FROM temp.snapshot_revisions '
                f"WHERE record_key='' AND table_name IN ({marks}) ORDER BY table_name", tables))
        else:
            revisions = conn.total_changes
        return id(conn), schema, external, revisions

    def _start_scan(self, conn, epoch):
        tables = []
        for table, filename in [*((table, 'recovery_filename') for table in OWNERS),
                                ('action_backups', 'retained_filename')]:
            columns = list(conn.execute(f'PRAGMA table_info({table})'))
            if not any(row[1] == filename for row in columns):
                continue
            keys = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
            if not keys:
                raise ValueError('Collection owner requires a primary key: ' + table)
            tables.append((table, filename, keys))
        self.reset()
        self._tables, self._epoch = tables, epoch

    def required(self, conn, names, page_size):
        epoch = self._ownership_epoch(conn)
        if epoch != self._epoch:
            self._start_scan(conn, epoch)
        marks = ','.join('?' for _ in names)
        while self._table_index < len(self._tables):
            check_cancellable_work()
            table, filename, keys = self._tables[self._table_index]
            fields = ','.join(keys)
            ordered = ','.join(key + ' DESC' for key in keys)
            seek = (f'WHERE ({fields}) > ({",".join("?" for _ in keys)}) '
                    if self._after is not None else '')
            rows = conn.execute(
                f'WITH page AS MATERIALIZED (SELECT {fields},{filename} FROM {table} '
                f'{seek}ORDER BY {fields} LIMIT ?) '
                f'SELECT {fields},{filename},0 AS marker FROM page WHERE {filename} IN ({marks}) '
                f'UNION ALL SELECT {fields},NULL,1 AS marker FROM '
                f'(SELECT {fields} FROM page ORDER BY {ordered} LIMIT 1) ORDER BY marker',
                (*(self._after or ()), page_size, *names)).fetchall()
            # An interrupted query retries this finite page, preserving earlier
            # pages. The epoch invalidates progress after an owner/key change.
            if not rows:
                self._table_index += 1
                self._after = None
            else:
                self._owned.update(row[-2] for row in rows[:-1])
                self._after = tuple(rows[-1][:-2])
        return self._owned


class CollectionRetentionRepo(Repo):
    @synchronized
    def active(self):
        return not self._store.activity_paused and any(row[1] == 'recovery_filename'
            for row in self.conn.execute('PRAGMA table_info(road_trip_drafts)'))

    @contextmanager
    def reservation(self):
        with self._lock:
            if self.conn.in_transaction:
                raise ValueError('Collection cleanup requires an idle connection')
            previous = self.conn.execute('PRAGMA busy_timeout').fetchone()[0]
            self.conn.execute('PRAGMA busy_timeout=0')
            try:
                if self._store.activity_paused:
                    yield False
                    return
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    yield True
                finally:
                    self.conn.rollback()
            finally:
                self.conn.execute(f'PRAGMA busy_timeout={previous}')

    @synchronized
    def required(self, scan, names, page_size):
        return scan.required(self.conn, names, page_size)
