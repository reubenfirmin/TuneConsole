"""Keep provider selections and corrections with their recordings."""
from itertools import islice
import re

from .. import legacy_journals, metadata_history as history, recording_metadata, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged

NAME = 'own_metadata_history_on_recordings'
RETIRED = 'catalog_metadata_audit'
REFRESH = 'recording_metadata_corrections_refresh'
REFERENCE = re.compile(r'\b' + RETIRED + r'\b')
OWNERS = ('track_sources', 'recordings')
CURSOR = 'prepare_metadata_history_after_id'
PAGE_SIZE = 512


def _objects(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name")
        if row[1] in OWNERS or row[2] == RETIRED or REFERENCE.search(row[3])]


def predecessor_objects(conn):
    return [sql for _, _, _, sql in _objects(conn)]


def preparing(conn):
    return worker_settings.read(conn, CURSOR) is not None


def _remaining(after):
    return ('id>?', (int(after),)) if after else ('1', ())


def _prepare_storage(conn):
    for column in (history.SELECTIONS, history.CORRECTIONS):
        conn.execute(f"ALTER TABLE recordings ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]' "
                     f"CHECK(json_type({column})='array')")
    corrected = history.CORRECTED_FIELDS
    conn.execute(f"ALTER TABLE track_sources ADD COLUMN {corrected} TEXT NOT NULL DEFAULT '{{}}' "
                 f"CHECK(json_type({corrected})='object')")
    worker_settings.save(conn, CURSOR, '')


def _copy_page(conn):
    where, args = _remaining(worker_settings.read(conn, CURSOR))
    boundary = conn.execute(f'SELECT max(id) FROM (SELECT id FROM {RETIRED} WHERE {where} ORDER BY id LIMIT ?)',
                            (*args, PAGE_SIZE)).fetchone()[0]
    if boundary is None:
        return False
    where += ' AND id<=?'
    args = (*args, boundary)
    event = history.event_sql('id', 'entry_id', 'field', 'source', 'operation', 'value', 'genre_key', 'changed_at')
    # A checkpoint written by an older version may already have been rewound.
    # Merge by event so retrying that checkpoint preserves identity and order.
    for column, predicate in ((history.SELECTIONS, "operation='select'"),
                              (history.CORRECTIONS, f'operation IN ({recording_metadata.OVERRIDES})')):
        conn.execute(f'''WITH by_recording AS (
            SELECT recording_id,json_group_array({event} ORDER BY id) AS events FROM {RETIRED}
            WHERE {where} AND {predicate} GROUP BY recording_id
            ) UPDATE recordings SET {column}=(SELECT json_group_array(json(value) ORDER BY value->>0) FROM (
                SELECT value FROM json_each(recordings.{column}) UNION SELECT value FROM json_each(by_recording.events)
            )) FROM by_recording WHERE recordings.id=by_recording.recording_id''', args)
    corrected = history.CORRECTED_FIELDS
    conn.execute(f'''WITH fields AS (
        SELECT DISTINCT entry_id,field FROM {RETIRED} WHERE {where} AND operation IN ({recording_metadata.OVERRIDES})
        ), by_entry AS (SELECT entry_id,json_group_object(field,1) AS corrected FROM fields GROUP BY entry_id)
        UPDATE track_sources SET {corrected}=json_patch({corrected},by_entry.corrected)
        FROM by_entry WHERE track_sources.id=by_entry.entry_id''', args)
    _archive_unowned_history(conn, event, where, args)
    worker_settings.save(conn, CURSOR, str(boundary))
    return True


def prepare(conn, version):
    while True:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
                return
            if not preparing(conn):
                _prepare_storage(conn)
            elif not _copy_page(conn):
                return


def install(conn, _context):
    after = worker_settings.read(conn, CURSOR)
    where, args = _remaining(after)
    if after is None or conn.execute(f'SELECT 1 FROM {RETIRED} WHERE {where} LIMIT 1', args).fetchone():
        raise PreparationChanged('Metadata history preparation is incomplete')
    conn.execute('DROP TRIGGER IF EXISTS prepare_metadata_history_recording_delete')
    worker_settings.save(conn, CURSOR, None)
    dependents = [(name, sql) for kind, name, owner, sql in _objects(conn)
                  if kind == 'trigger' and owner != RETIRED and REFERENCE.search(sql)]
    for name, _ in dependents:
        conn.execute(f'DROP TRIGGER {name}')
    high_water = conn.execute(f'''SELECT max(COALESCE((SELECT max(id) FROM {RETIRED}),0),
        COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0))''', (RETIRED,)).fetchone()[0]
    if high_water:
        worker_settings.save(conn, history.SERIAL, str(high_water))
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'CREATE VIEW {history.VIEW} AS {history.events_sql(history.SELECTIONS)} UNION ALL '
                 + history.events_sql(history.CORRECTIONS))
    before = recording_metadata.live_choice_sql(recording_metadata._choice_inputs('track_sources'))
    after = recording_metadata.live_choice_sql(recording_metadata._choice_inputs('track_sources', owned_history=True))
    for name, sql in dependents:
        if before not in sql:
            raise MigrationError('Unrecognized recording metadata history read in ' + name)
        conn.execute(sql.replace(before, after))
    refresh = recording_metadata.live_choice_sql(recording_metadata._refresh('NEW.id', folded=True, owned_history=True))
    fields = ','.join(f"'{field}'" for field in recording_metadata.CANONICAL_FIELDS)
    column = history.CORRECTIONS
    conn.execute(f'''CREATE TRIGGER {REFRESH} AFTER UPDATE OF {column} ON recordings
        WHEN NEW.{column} IS NOT OLD.{column} AND (NEW.{column}->'$[#-1]')->>2 IN ({fields})
        BEGIN {refresh} END''')


def _archive_unowned_history(conn, event, where, args):
    missing = conn.execute(f'''SELECT id,json_object('recording_id',recording_id,'event',{event}) FROM {RETIRED}
        WHERE {where} AND NOT EXISTS(SELECT 1 FROM recordings WHERE id={RETIRED}.recording_id) ORDER BY id''', args)
    while batch := list(islice(missing, 64)):
        legacy_journals.retain_many(conn, 'metadata', [('recording_metadata_history', str(identity), value,
            'Historical metadata event whose recording is absent') for identity, value in batch])


def validate(conn):
    names = {history.VIEW, REFRESH}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Metadata history was not moved to its recordings')
