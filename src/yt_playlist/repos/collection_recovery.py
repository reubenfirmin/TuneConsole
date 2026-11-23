"""SQL references and guarded replacement for collection recovery."""
from contextlib import contextmanager

from yt_playlist.core import model_state, spotify_records
from yt_playlist.core.collection_owners import (
    OWNERS, OWNER_KEYS, PLAYLIST_DOCUMENTS, PREDECESSOR_CLEANUP, PREDECESSOR_OWNERS, SYNC_ATTEMPT, TASTE_CONTROLS,
)
from yt_playlist.repos.connection import ConnectionRepo


INLINE_WORKFLOWS = ('tv_append_intents', 'experiment_decisions')
PREDECESSOR_TABLES = {'road_trip_recipes': 'road_trip_drafts', 'sync_cursors': 'sync_attempts'}
WORKFLOW_PREDECESSORS = {'sync_cursors', 'discovered_artists', *PREDECESSOR_OWNERS,
                         *INLINE_WORKFLOWS, *spotify_records.ARCHIVE_PREFIXES}


def references(conn, *, predecessor_model_state=False, predecessor_playlist_documents=False,
               predecessor_playlist_cleanup=False, predecessor_taste_controls=False, predecessor_workflows=frozenset()):
    owners = list(OWNERS.items())
    if predecessor_playlist_cleanup:
        owners.append(PREDECESSOR_CLEANUP)
    separate_action_drafts = 'action_drafts' in predecessor_workflows
    if separate_action_drafts:
        owners.append(('action_drafts', PREDECESSOR_OWNERS['action_drafts']))
    for table, kind in owners:
        predecessor = PREDECESSOR_TABLES.get(table)
        if predecessor in predecessor_workflows:
            table = predecessor
        if table == 'sync_cursors' and table not in predecessor_workflows:
            row = conn.execute("""SELECT value->>'recovery_filename',value->>'recovery_digest',
                value->>'recovery_bytes' FROM settings WHERE key=?""", (SYNC_ATTEMPT,)).fetchone()
            if row is not None and row[0] is not None:
                yield table, 1, kind, *row
            continue
        if table in spotify_records.ARCHIVE_PREFIXES and table not in predecessor_workflows:
            for _, identity, *reference in spotify_records.references(conn, spotify_records.ARCHIVE_PREFIXES[table]):
                yield table, identity, kind, *reference
            continue
        if table in INLINE_WORKFLOWS and table not in predecessor_workflows:
            continue
        if table == 'taste_model_state' and not predecessor_model_state:
            for identity, reference in model_state.references(conn):
                yield table, identity, kind, *reference
            continue
        if table in TASTE_CONTROLS and not predecessor_taste_controls:
            row = conn.execute("""SELECT value->>'recovery_filename',value->>'recovery_digest',
                value->>'recovery_bytes' FROM settings WHERE key=?""", (TASTE_CONTROLS[table],)).fetchone()
            if row is not None and row[0] is not None:
                yield table, 1, kind, *row
            continue
        if table in PLAYLIST_DOCUMENTS and not predecessor_playlist_documents:
            prefix = PLAYLIST_DOCUMENTS[table]
            for identity, filename, digest, size in conn.execute(
                    f'SELECT {prefix}_id,{prefix}_recovery_filename,{prefix}_recovery_digest,'
                    f'{prefix}_recovery_bytes FROM playlist_scopes '
                    f'WHERE {prefix}_recovery_filename IS NOT NULL ORDER BY {prefix}_id'):
                yield table, identity, kind, filename, digest, size
            continue
        key = OWNER_KEYS.get(table, 'id')
        folded_actions = table == 'actions' and not separate_action_drafts
        owner_kind = "CASE WHEN kind='merge_draft' THEN 'action-draft' ELSE 'action' END" if folded_actions else '?'
        parameters = () if folded_actions else (kind,)
        for identity, kind, filename, digest, size in conn.execute(
                f'SELECT {key},{owner_kind},recovery_filename,recovery_digest,recovery_bytes FROM {table} '
                f'WHERE recovery_filename IS NOT NULL ORDER BY {key}', parameters):
            yield table, identity, kind, filename, digest, size


class CollectionRecovery(ConnectionRepo):
    def __init__(self, conn, *, predecessor_model_state=False, predecessor_playlist_documents=False,
                 predecessor_playlist_cleanup=False, predecessor_taste_controls=False, predecessor_workflows=frozenset()):
        super().__init__(conn)
        self._predecessor_model_state = predecessor_model_state
        self._predecessor_playlist_documents = predecessor_playlist_documents
        self._predecessor_playlist_cleanup = predecessor_playlist_cleanup
        self._predecessor_taste_controls = predecessor_taste_controls
        self._predecessor_workflows = predecessor_workflows

    @property
    def pinned(self):
        with self._lock:
            return self.conn.in_transaction

    def references(self):
        with self._lock:
            yield from references(self.conn, predecessor_model_state=self._predecessor_model_state,
                                  predecessor_playlist_documents=self._predecessor_playlist_documents,
                                  predecessor_playlist_cleanup=self._predecessor_playlist_cleanup,
                                  predecessor_taste_controls=self._predecessor_taste_controls,
                                  predecessor_workflows=self._predecessor_workflows)

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
        with self._lock:
            if not self.conn.in_transaction:
                raise ValueError('Coordinate relocation requires a restore transaction')
            for (table, identity), filename in files.items():
                if (table not in OWNERS and not (table in PREDECESSOR_OWNERS and table in self._predecessor_workflows)
                        and not (self._predecessor_playlist_cleanup and table == PREDECESSOR_CLEANUP[0])):
                    raise ValueError('Unknown collection recovery owner')
                if table == 'discovered_artists' and table not in self._predecessor_workflows:
                    self.conn.execute('UPDATE artists SET discovery_recovery_filename=? '
                                      'WHERE id=? AND discovery_candidate=1', (filename, identity))
                elif table == 'taste_model_state' and not self._predecessor_model_state:
                    model_state.relocate(self.conn, identity, filename)
                elif table == 'sync_cursors' and table not in self._predecessor_workflows:
                    self.conn.execute("UPDATE settings SET value=json_set(value,'$.recovery_filename',?) "
                                      "WHERE key=? AND ?=1", (filename, SYNC_ATTEMPT, identity))
                elif table in spotify_records.ARCHIVE_PREFIXES and table not in self._predecessor_workflows:
                    prefix = spotify_records.ARCHIVE_PREFIXES[table]
                    self.conn.execute("UPDATE settings SET value=json_set(value,'$.recovery_filename',?) "
                                      "WHERE key GLOB ? AND value->>'id'=?", (filename, prefix + '*', identity))
                elif table in TASTE_CONTROLS and not self._predecessor_taste_controls:
                    self.conn.execute("UPDATE settings SET value=json_set(value,'$.recovery_filename',?) "
                                      "WHERE key=? AND ?=1", (filename, TASTE_CONTROLS[table], identity))
                elif table in PLAYLIST_DOCUMENTS and not self._predecessor_playlist_documents:
                    prefix = PLAYLIST_DOCUMENTS[table]
                    self.conn.execute(f'UPDATE playlist_scopes SET {prefix}_recovery_filename=? '
                                      f'WHERE {prefix}_id=?', (filename, identity))
                else:
                    key = OWNER_KEYS.get(table, 'id')
                    self.conn.execute(f'UPDATE {table} SET recovery_filename=? WHERE {key}=?', (filename, identity))
