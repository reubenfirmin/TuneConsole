"""Resumable SQL ownership checks for a bounded collection-file batch."""
from contextlib import contextmanager

from yt_playlist.core import action_backup_inventory, model_state, spotify_records, sync_state
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.collection_owners import OWNERS, OWNER_KEYS, PLAYLIST_DOCUMENTS, TASTE_CONTROLS
from yt_playlist.core.collection_recovery_epoch import revision
from .base import Repo, synchronized


FILE_OWNERS = (
    # Page recipes by their non-null primary key, including rows without drafts.
    *((table, 'recovery_filename', ('id' if table == 'road_trip_recipes' else OWNER_KEYS.get(table, 'id'),))
      for table in OWNERS if table not in ('taste_model_state', 'tv_append_intents', 'sync_cursors', 'experiment_decisions')
      and table not in PLAYLIST_DOCUMENTS and table not in TASTE_CONTROLS
      and table not in spotify_records.ARCHIVE_PREFIXES),
    *(('playlist_scopes', f'{prefix}_recovery_filename', ('id',)) for prefix in PLAYLIST_DOCUMENTS.values()),
    ('actions', action_backup_inventory.COLUMN, ('id',)),
)


class OwnershipScan:
    def __init__(self):
        self.reset()

    def reset(self):
        self._epoch = None
        self._table_index = 0
        self._after = None
        self._owned = set()

    def required(self, conn, names, page_size):
        epoch = conn, revision(conn)
        if epoch != self._epoch:
            self.reset()
            self._epoch = epoch
        self._owned.update(reference[0] for _, reference in model_state.references(conn) if reference[0] in names)
        self._owned.update(filename for filename, in conn.execute(
            "SELECT value->>'recovery_filename' FROM settings WHERE key IN (?,?,?)",
            (*TASTE_CONTROLS.values(), sync_state.ATTEMPT))
            if filename in names)
        self._owned.update(filename for prefix in spotify_records.ARCHIVE_PREFIXES.values()
                           for _, _, filename, _, _ in spotify_records.references(conn, prefix) if filename in names)
        marks = ','.join('?' for _ in names)
        while self._table_index < len(FILE_OWNERS):
            check_cancellable_work()
            table, filename, keys = FILE_OWNERS[self._table_index]
            fields = ','.join(keys)
            ordered = ','.join(key + ' DESC' for key in keys)
            seek = (f'WHERE ({fields}) > ({",".join("?" for _ in keys)}) '
                    if self._after is not None else '')
            source = 'page'
            retained = filename
            if filename == action_backup_inventory.COLUMN:
                source = f'page JOIN json_each(page.{filename}) AS item'
                retained = "item.value->>'retained_filename'"
            rows = conn.execute(
                f'WITH page AS MATERIALIZED (SELECT {fields},{filename} FROM {table} '
                f'{seek}ORDER BY {fields} LIMIT ?) '
                f'SELECT {",".join(f"page.{key}" for key in keys)},{retained},0 AS marker FROM {source} '
                f'WHERE {retained} IN ({marks}) '
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
        return not self._store.activity_paused

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
