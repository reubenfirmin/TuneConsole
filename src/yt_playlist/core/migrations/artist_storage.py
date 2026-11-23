"""Fold artist attributes and observation selections into their owning rows."""
from functools import lru_cache
import json
import re

from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'fold_artist_storage'
TABLES = frozenset(('artist_ambiguities', 'artist_scans', 'artist_redirects', 'rec_artist_similar',
                    'artist_credit_capture_heads', 'entry_artist_selections', 'artist_credit_retention'))
RETENTION_KEY = 'artist_credit_retention'
ARTIST_COLUMNS = (
    'contextual_credit INTEGER NOT NULL DEFAULT 0 CHECK(contextual_credit IN (0,1))',
    'discography_scanned_at REAL',
    'redirect_artist_id INTEGER REFERENCES artists(id) CHECK(redirect_artist_id<>id)',
    'redirect_source TEXT',
    'redirect_created_at REAL CHECK((redirect_artist_id IS NULL AND redirect_source IS NULL AND redirect_created_at IS NULL) OR '
    '(redirect_artist_id IS NOT NULL AND redirect_source IS NOT NULL AND redirect_created_at IS NOT NULL))',
    'lastfm_similar_json TEXT',
    'lastfm_fetched_at REAL CHECK(lastfm_fetched_at IS NULL OR lastfm_similar_json IS NOT NULL)',
)
OBSERVATION_COLUMNS = (
    'is_capture_head INTEGER NOT NULL DEFAULT 0 CHECK(is_capture_head IN (0,1))',
    'is_selected INTEGER NOT NULL DEFAULT 0 CHECK(is_selected IN (0,1))',
)
MARKER = 'uq_artist_credit_capture_head'
INDEXES = (
    ('ix_artists_redirect_target', 'CREATE INDEX ix_artists_redirect_target ON artists(redirect_artist_id) '
     'WHERE redirect_artist_id IS NOT NULL'),
    (MARKER, f'CREATE UNIQUE INDEX {MARKER} ON artist_credit_observations(entry_id,source) WHERE is_capture_head=1'),
    ('uq_artist_credit_selection', 'CREATE UNIQUE INDEX uq_artist_credit_selection '
     'ON artist_credit_observations(entry_id) WHERE is_selected=1'),
)

_BAD_REDIRECT = (
    'NEW.redirect_artist_id IS NOT NULL AND (NEW.provisional<>1 '
    'OR EXISTS(SELECT 1 FROM artist_provider_ids WHERE artist_id=NEW.id) '
    'OR NOT EXISTS(SELECT 1 FROM artists a JOIN artist_provider_ids p ON p.artist_id=a.id '
    'WHERE a.id=NEW.redirect_artist_id AND a.provisional=0 AND a.redirect_artist_id IS NULL))'
)
_REJECT_REDIRECT = "BEGIN SELECT RAISE(ABORT,'Artist redirect requires a provisional source and canonical target'); END"
_INVALIDATE_SUMMARY = (
    'BEGIN INSERT INTO listening_summary_state(id,input_revision) VALUES (1,1) '
    'ON CONFLICT(id) DO UPDATE SET input_revision=input_revision+1; END'
)
TRIGGERS = (
    ('artist_redirect_insert', f'CREATE TRIGGER artist_redirect_insert BEFORE INSERT ON artists '
     f'WHEN {_BAD_REDIRECT} {_REJECT_REDIRECT}'),
    ('artist_redirect_update', f'CREATE TRIGGER artist_redirect_update BEFORE UPDATE OF redirect_artist_id ON artists '
     f'WHEN {_BAD_REDIRECT} {_REJECT_REDIRECT}'),
    ('listening_summary_artist_redirects_insert', 'CREATE TRIGGER listening_summary_artist_redirects_insert '
     f'AFTER INSERT ON artists WHEN NEW.redirect_artist_id IS NOT NULL {_INVALIDATE_SUMMARY}'),
    ('listening_summary_artists_update', 'CREATE TRIGGER listening_summary_artists_update '
     'AFTER UPDATE OF id,name,provisional,redirect_artist_id ON artists '
     'WHEN NEW.id IS NOT OLD.id OR NEW.name IS NOT OLD.name OR NEW.provisional IS NOT OLD.provisional '
     f'OR NEW.redirect_artist_id IS NOT OLD.redirect_artist_id {_INVALIDATE_SUMMARY}'),
    ('retained_artist_credit_observation', 'CREATE TRIGGER retained_artist_credit_observation '
     'BEFORE DELETE ON artist_credit_observations WHEN OLD.is_selected=1 OR OLD.is_capture_head=1 '
     "BEGIN SELECT RAISE(ABORT,'Current artist evidence cannot be deleted'); END"),
)
EMBEDDING_TABLES = ('artist_embeddings', 'artist_embedding_files')
EMBEDDING_TRIGGERS = tuple(
    (f'{table}_owner_{operation.lower()}',
     f'CREATE TRIGGER {table}_owner_{operation.lower()} BEFORE {operation} ON {table} '
     'WHEN EXISTS(SELECT 1 FROM artists WHERE id=NEW.artist_id AND redirect_artist_id IS NOT NULL) '
     "BEGIN SELECT RAISE(ABORT,'Embedding owner is retired'); END")
    for table in EMBEDDING_TABLES for operation in ('INSERT', 'UPDATE')
)
RETIRE_EMBEDDINGS = tuple(
    (f'retire_{table}_{suffix}', f'CREATE TRIGGER retire_{table}_{suffix} AFTER {operation} ON artists '
     f'WHEN NEW.redirect_artist_id IS NOT NULL BEGIN DELETE FROM {table} WHERE artist_id=NEW.id; END')
    for table in EMBEDDING_TABLES
    for suffix, operation in (('insert', 'INSERT'), ('update', 'UPDATE OF redirect_artist_id'))
)
LISTENING_VIEW = 'recording_listening_artists'


def _listening_view(*, compact, unified):
    from . import schema_definition

    sql = next(sql for sql in schema_definition.statements() if sql.startswith(f'CREATE VIEW {LISTENING_VIEW} ')).rstrip(';')
    if compact:
        sql = sql.replace('coalesce(r.artist_id,c.artist_id)', 'coalesce(r.redirect_artist_id,c.artist_id)')
        sql = sql.replace('LEFT JOIN artist_redirects r ON r.retired_id=c.artist_id',
                          'JOIN artists r ON r.id=c.artist_id')
    return sql.replace('FROM catalog_entries ', 'FROM track_sources ') if unified else sql


def install(conn, _context):
    for table, columns in (('artists', ARTIST_COLUMNS), ('artist_credit_observations', OBSERVATION_COLUMNS)):
        for column in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column}')
    conn.execute('DROP TRIGGER listening_summary_artists_update')
    conn.execute(f'DROP VIEW {LISTENING_VIEW}')
    for name, _ in (*EMBEDDING_TRIGGERS, *RETIRE_EMBEDDINGS):
        conn.execute(f'DROP TRIGGER {name}')
    conn.execute('''
        UPDATE artists SET
            contextual_credit=EXISTS(SELECT 1 FROM artist_ambiguities WHERE artist_id=artists.id),
            discography_scanned_at=(SELECT scanned_at FROM artist_scans WHERE artist_id=artists.id),
            (redirect_artist_id,redirect_source,redirect_created_at)=
                (SELECT artist_id,source,created_at FROM artist_redirects WHERE retired_id=artists.id),
            (lastfm_similar_json,lastfm_fetched_at)=
                (SELECT payload,fetched_at FROM rec_artist_similar WHERE artist_id=artists.id)
        WHERE id IN (SELECT artist_id FROM artist_ambiguities
                     UNION SELECT artist_id FROM artist_scans
                     UNION SELECT retired_id FROM artist_redirects
                     UNION SELECT artist_id FROM rec_artist_similar)
    ''')
    conn.execute('UPDATE artist_credit_observations SET is_capture_head=1 '
                 'WHERE id IN (SELECT observation_id FROM artist_credit_capture_heads)')
    conn.execute('UPDATE artist_credit_observations SET is_selected=1 '
                 'WHERE id IN (SELECT observation_id FROM entry_artist_selections)')
    checkpoint_fields = ('enabled', 'cursor', 'completed', 'next_scan_at')
    checkpoint = conn.execute('SELECT ' + ','.join(checkpoint_fields) +
                              ' FROM artist_credit_retention WHERE id=1').fetchone()
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                 (RETENTION_KEY, json.dumps(dict(zip(checkpoint_fields, checkpoint)))))
    for table in sorted(TABLES):
        conn.execute(f'DROP TABLE {table}')
    conn.execute(_listening_view(compact=True, unified=True))
    for _, sql in (*INDEXES, *TRIGGERS, *EMBEDDING_TRIGGERS, *RETIRE_EMBEDDINGS):
        conn.execute(sql)


def validate(conn):
    from . import artist_details

    names = {name for name, _ in (*INDEXES, *TRIGGERS, *EMBEDDING_TRIGGERS, *RETIRE_EMBEDDINGS)} | {LISTENING_VIEW}
    if definitions(conn, (artist_details.MARKER,)):
        names = {name for name in names if not name.startswith(('artist_embeddings_', 'retire_artist_embeddings_'))}
    if definitions(conn, ('ix_play_event_day',)):
        names = {name for name in names if not name.startswith('listening_summary_')}
    found = definitions(conn, TABLES | names)
    if TABLES.intersection(found) or not names <= found.keys():
        raise MigrationError('Artist storage was not folded')
    if 'credit_enabled' in tokens(definitions(conn, ('taste_mode_retention',)).get('taste_mode_retention')):
        checkpoint = conn.execute('SELECT 1 FROM taste_mode_retention WHERE id=1').fetchone()
    else:
        checkpoint = conn.execute('SELECT 1 FROM settings WHERE key=? AND json_valid(value)', (RETENTION_KEY,)).fetchone()
    if checkpoint is None:
        raise MigrationError('Missing artist credit retention state')


@lru_cache(maxsize=3)
def contract(*, compact, unified):
    from .. import credit_retention, embedding_files, track_sources
    from . import schema_definition

    ddl = []
    added = {'artists': ARTIST_COLUMNS, 'artist_credit_observations': OBSERVATION_COLUMNS}
    for sql in schema_definition.statements():
        table = re.match(r'(?:CREATE|ALTER) TABLE "?(\w+)', sql)
        if table and table[1] in added:
            ddl.append(sql)
        elif not compact and (table and table[1] in TABLES or
                sql.startswith('CREATE TRIGGER listening_summary_artists_update ') or
                sql.startswith(('CREATE TRIGGER ', 'CREATE INDEX ', 'CREATE UNIQUE INDEX ')) and
                any(re.search(rf'\bON "?{name}"?(?:\s|\()', sql) for name in TABLES)):
            ddl.append(sql)
    if compact:
        ddl.extend(f'ALTER TABLE {table} ADD COLUMN {column}'
                   for table, columns in added.items() for column in columns)
        ddl.extend(sql for _, sql in (*INDEXES, *TRIGGERS))
    else:
        ddl.extend(sql for sql in credit_retention.statements()
                   if sql.startswith('CREATE TABLE artist_credit_retention '))
    expected = schema_definition._schema_contract(
        track_sources.folded_foreign_keys(sql) if unified else sql for sql in ddl)
    embedding_triggers = (*EMBEDDING_TRIGGERS, *RETIRE_EMBEDDINGS)
    if not compact:
        names = {name for name, _ in embedding_triggers}
        embedding_triggers = [(sql.split()[2], sql) for sql in schema_definition.statements()
                              if sql.startswith('CREATE TRIGGER ') and sql.split()[2] in names]
        embedding_triggers.extend((name, sql) for name, sql in embedding_files.definitions() if name in names)
    expected.update((name, ('trigger', tokens(sql.rstrip(';')))) for name, sql in embedding_triggers)
    expected[LISTENING_VIEW] = ('view', tokens(_listening_view(compact=compact, unified=unified)))
    return expected
