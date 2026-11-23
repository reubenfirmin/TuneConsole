"""Remove retired catalog storage and keep original metadata on its delivery."""
import re

from .. import legacy_journals, track_sources
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'retire_legacy_catalog_storage'
ARCHIVE_TABLES = ('legacy_archive_heads', *legacy_journals.EARLIER_PACK_RECEIPTS.values())
TRACK_IDS = 'legacy_track_refs'
ORIGINALS = 'metadata_legacy_records'
WORKFLOW_RECEIPTS = legacy_journals.EARLIER_PACK_RECEIPTS['workflow']
DELIVERIES, OBSERVATIONS = 'metadata_deliveries', 'metadata_observations'
METADATA = (DELIVERIES, OBSERVATIONS)
PREFIX = '_retired_catalog_'
ORIGINAL_INDEX = 'uq_metadata_delivery_original'
STAGED_INDEX_TARGET = f'{ORIGINAL_INDEX} ON {PREFIX}{DELIVERIES}(original_owner,original_id)'
CURSOR = 'migration:retired-catalog:cursor:'
CHANGED = 'migration:retired-catalog:changed:'
PAGE_SIZE = 256
OWNERS = ('metadata_findings', 'enrichment_log', 'metadata_decisions', 'entry_field_choices',
          'enrichment_conflict', 'recording_identifiers', 'provider_attempts',
          'enrichment_provider_state', 'catalog_field_hints')
DISPOSITIONS = ('catalog_evidence', 'native_observation', 'unsupported_field', 'invalid_value',
                'unproven_subject', 'unproven_scope', 'operational_state')
ORIGINAL_COLUMNS = {
    'original_owner': 'TEXT',
    'original_id': 'INTEGER',
    'original_entry_id': 'INTEGER REFERENCES track_sources(id)',
    'original_recording_id': 'INTEGER REFERENCES recordings(id)',
    'original_video_id': 'TEXT REFERENCES track_sources(video_id)',
    'original_disposition': 'TEXT CHECK((original_owner IS NULL AND original_id IS NULL AND '
        'original_entry_id IS NULL AND original_recording_id IS NULL AND original_video_id IS NULL AND '
        'original_disposition IS NULL) OR (original_owner IS NOT NULL AND '
        f'original_owner IN {OWNERS} AND typeof(original_id)=\'integer\' AND '
        f'original_disposition IS NOT NULL AND original_disposition IN {DISPOSITIONS} AND '
        "key_contract='legacy_record'))",
}
ORIGINAL_GUARD = ('CREATE TRIGGER immutable_metadata_delivery_original BEFORE UPDATE OF '
                  + ','.join(ORIGINAL_COLUMNS) + ' ON metadata_deliveries '
                  "BEGIN SELECT RAISE(ABORT,'Original metadata provenance is immutable'); END")
RETAIN_ORIGINAL = ('CREATE TRIGGER retained_metadata_delivery_original BEFORE DELETE ON metadata_deliveries '
                   'WHEN OLD.original_owner IS NOT NULL '
                   "BEGIN SELECT RAISE(ABORT,'Original metadata provenance requires a retention disposition'); END")
OBSERVATION_FK = 'REFERENCES metadata_legacy_records(legacy_owner,legacy_id)'
REFERENCES = ('enrichment_conflict', 'owned_legacy_metadata_conflict',
              'owned_metadata_observation_delivery', 'owned_native_metadata_legacy',
              'owned_provider_state_field_insert', 'owned_provider_state_field_update',
              'owned_recording_identifier_delivery_insert', 'owned_recording_identifier_delivery_update')


def predecessor_definitions():
    from . import schema_definition

    prior = {'metadata_deliveries', 'metadata_observations', *REFERENCES}
    return [track_sources.folded_sql(sql.rstrip(';')) for sql in schema_definition.statements()
            if any(sql.startswith(f'CREATE {kind} {name} ') for name in prior
                   for kind in ('TABLE', 'TRIGGER', 'VIEW'))]


def _metadata_reference(sql):
    sql = sql.replace('r.input_video_id,c.field_scope', 'r.input_video_id AS input_video_id,c.field_scope')
    sql = sql.replace('metadata_legacy_records r', 'metadata_deliveries r')
    for old, new in zip(('legacy_owner', 'legacy_id', 'entry_id', 'recording_id', 'input_video_id', 'delivery_id'),
                        ('original_owner', 'original_id', 'original_entry_id', 'original_recording_id',
                         'original_video_id', 'id')):
        sql = sql.replace('r.' + old, 'r.' + new)
    return sql


def _create(conn):
    for table, sql in definitions(conn, METADATA).items():
        if table == OBSERVATIONS:
            sql = sql.replace(OBSERVATION_FK, f'REFERENCES {PREFIX}{DELIVERIES}(original_owner,original_id)')
            for reference in METADATA:
                sql = sql.replace(f'REFERENCES {reference}(', f'REFERENCES {PREFIX}{reference}(')
        conn.execute(re.sub(r'^CREATE TABLE "?\w+"?', f'CREATE TABLE "{PREFIX}{table}"', sql, count=1))
        conn.execute('INSERT INTO settings(key,value) VALUES (?,NULL)', (CURSOR + table,))
    for name, declaration in ORIGINAL_COLUMNS.items():
        conn.execute(f'ALTER TABLE {PREFIX}{DELIVERIES} ADD COLUMN {name} {declaration}')
    _ensure_original_index(conn)


def _ensure_original_index(conn):
    # Staged observations already reference this parent key. SQLite retains
    # the index when the delivery table takes its final name at cutover.
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ' + STAGED_INDEX_TARGET)


def _copy(conn, table, selected, args):
    source = f'{table} AS d'
    columns = 'd.*'
    if table == DELIVERIES:
        if conn.execute(f'SELECT 1 FROM {source} JOIN {ORIGINALS} o ON o.delivery_id=d.id '
                        f'WHERE {selected} GROUP BY d.id HAVING count(*)>1 LIMIT 1', args).fetchone():
            raise MigrationError('Multiple original metadata records reference one delivery')
        source += f' LEFT JOIN {ORIGINALS} o ON o.delivery_id=d.id'
        columns += ',o.legacy_owner,o.legacy_id,o.entry_id,o.recording_id,o.input_video_id,o.disposition'
    conn.execute(f'INSERT OR REPLACE INTO {PREFIX}{table} SELECT {columns} FROM {source} WHERE {selected}', args)


def _scan_page(conn, table, after):
    where = 'WHERE id>?' if after is not None else ''
    page = conn.execute(f'SELECT id FROM {table} {where} ORDER BY id LIMIT {PAGE_SIZE}',
                        () if after is None else (after,)).fetchall()
    if page:
        _copy(conn, table, 'd.id BETWEEN ? AND ?', (page[0][0], page[-1][0]))
    conn.execute('UPDATE settings SET value=? WHERE key=?', (page[-1][0] if page else 'done', CURSOR + table))


def _cursors(conn):
    return dict(conn.execute('SELECT substr(key,?),value FROM settings WHERE key GLOB ?',
                             (len(CURSOR) + 1, CURSOR + '*')))


def _has_metadata(conn):
    return any(conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in METADATA)


def _require_prepared(conn):
    if _pending_workflow(conn):
        raise PreparationChanged('Workflow originals still need preparation')
    cursors = _cursors(conn)
    if not cursors and not _has_metadata(conn):
        _create(conn)
        return
    if set(cursors) != set(METADATA) or any(cursor != 'done' for cursor in cursors.values()):
        raise PreparationChanged('Metadata evidence preparation is incomplete')


def _fold_metadata(conn):
    sequences = dict(conn.execute('SELECT name,seq FROM sqlite_sequence WHERE name IN (?,?)', METADATA))
    restore = []
    for kind, name, table, sql in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        "(type IN ('view','trigger') OR (type='index' AND tbl_name IN (?,?))) "
        "AND name NOT GLOB ? ORDER BY CASE type WHEN 'index' THEN 0 WHEN 'view' THEN 1 ELSE 2 END,name",
        (*METADATA, PREFIX + '*'),
    ).fetchall():
        if table == ORIGINALS:
            continue
        if table in METADATA or name in REFERENCES or re.search(r'\b(metadata_deliveries|metadata_observations)\b', sql):
            restore.append(_metadata_reference(sql) if name in REFERENCES else sql)
            if table not in METADATA or kind == 'view':
                conn.execute(f'DROP {kind} "{name}"')

    # The staged tables refer to each other until their canonical names are
    # restored. Recreate the temporarily removed views and triggers afterwards.
    for table in (*reversed(METADATA), ORIGINALS):
        conn.execute(f'DROP TABLE {table}')
    conn.execute('PRAGMA legacy_alter_table=OFF')
    try:
        for table in METADATA:
            conn.execute(f'ALTER TABLE "{PREFIX}{table}" RENAME TO "{table}"')
    finally:
        conn.execute('PRAGMA legacy_alter_table=ON')
    for table, highest in sequences.items():
        _preserve_sequence(conn, table, highest)
    for sql in (*restore, ORIGINAL_GUARD, RETAIN_ORIGINAL):
        conn.execute(sql)
    conn.execute('DELETE FROM settings WHERE key GLOB ?', (CURSOR + '*',))


def _preserve_sequence(conn, table, highest):
    conn.execute('INSERT INTO sqlite_sequence(name,seq) SELECT ?,? '
                 'WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name=?)', (table, highest, table))
    conn.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (highest, table))


def _pending_workflow(conn):
    return WORKFLOW_RECEIPTS in definitions(conn, (WORKFLOW_RECEIPTS,)) and conn.execute(
        f'SELECT 1 FROM {WORKFLOW_RECEIPTS} LIMIT 1').fetchone() is not None


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    if _pending_workflow(conn):
        legacy_journals.prepare_batch(conn, 'workflow')
        return True
    cursors = _cursors(conn)
    if not cursors:
        if not _has_metadata(conn):
            return False
        _create(conn)
        return True
    _ensure_original_index(conn)
    for table in METADATA:
        if conn.execute('DELETE FROM settings WHERE key GLOB ?', (CHANGED + table + ':*',)).rowcount:
            conn.execute(f'DELETE FROM {PREFIX}{table}')
            conn.execute('UPDATE settings SET value=NULL WHERE key=?', (CURSOR + table,))
            return True
        if cursors[table] != 'done':
            _scan_page(conn, table, cursors[table])
            return True
    return False


def prepare(conn, version):
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    # A page may precede a parent referenced by a later page. Both staged
    # tables are checked after they replace their predecessors atomically.
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        more = True
        while more:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                more = _prepare_page(conn, version)
    finally:
        conn.execute(f'PRAGMA foreign_keys={foreign_keys}')


def install(conn, _context):
    _require_prepared(conn)
    for domain, setting in legacy_journals.RETAINED_PUBLICATIONS.items():
        publication = legacy_journals.head(conn, domain)
        if publication is not None:
            conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (setting, publication))
    for table in ARCHIVE_TABLES:
        conn.execute(f'DROP TABLE IF EXISTS {table}')
    legacy_journals._refresh_file_references(conn)
    highest = conn.execute(f'SELECT max(legacy_id) FROM {TRACK_IDS}').fetchone()[0]
    if highest is not None:
        # A retired key must not be allocated to a different source later.
        _preserve_sequence(conn, 'track_sources', highest)
    conn.execute(f'DROP TABLE {TRACK_IDS}')
    _fold_metadata(conn)


def validate(conn):
    if (definitions(conn, (*ARCHIVE_TABLES, TRACK_IDS, ORIGINALS)) or
            conn.execute('SELECT 1 FROM sqlite_schema WHERE name GLOB ? LIMIT 1', (PREFIX + '*',)).fetchone()):
        raise MigrationError('Retired catalog storage is still installed')
