"""Store play dates and provenance on events, and discard duplicate listening tables."""
from .. import group_preview_work, listening_summaries, listening_views, recording_library
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions, objects
from . import play_event_preparation


NAME = 'compact_play_event_storage'
PROJECTION_NAME = 'read_inline_play_event_evidence'
EVIDENCE_VIEWS = ('listening_evidence', 'eligible_listening_evidence', 'listening_activity')
MARKER = 'ix_play_event_day'
PROJECTIONS = ('play_event_calendar', 'play_event_sources', 'recording_listening_summary',
               'artist_listening_summary', 'trend_first_play')
REMOVED = ('listening_capture_receipts', 'listening_choice_windows', 'listening_summary_state',
           'listening_output_chunks', 'listening_output_heads')
FK_OWNERS = ('play_events', 'listening_observations', 'listening_output_files',
             'listening_output_publications', *PROJECTIONS[:-1], *REMOVED)
CAPTURE_INDEX = 'ix_listening_capture_account_time'
CALENDAR_INDEX = 'ix_play_event_calendar_day'
SOURCE_INDEX = 'ix_play_event_source'
OUTPUT_RETIRED_TRIGGERS = (
    'listening_output_head_insert', 'listening_output_head_update',
    'listening_output_chunk_insert', 'listening_output_chunk_update', 'listening_output_chunk_delete',
)
OUTPUT_CHANGED_TRIGGERS = ('listening_output_transition', 'listening_output_file_update')
CHANGE_TRIGGERS = ('rec_capture_play_events_update', 'rec_evidence_play_events_update',
                   'recording_library_play_events_update', 'group_preview_changed_play_events_update',
                   'group_preview_changed_listening_observations_update')
PREDECESSOR_OBJECTS = {
    'play_events', 'listening_observations', 'listening_output_files', 'listening_output_publications',
    *PROJECTIONS, *REMOVED, CAPTURE_INDEX, CALENDAR_INDEX, SOURCE_INDEX,
    *OUTPUT_RETIRED_TRIGGERS, *OUTPUT_CHANGED_TRIGGERS, *CHANGE_TRIGGERS,
}


def predecessor_objects(conn):
    return [sql for name, kind, sql in conn.execute(
        'SELECT name,type,sql FROM main.sqlite_schema WHERE sql IS NOT NULL ORDER BY '
        "CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 WHEN 'index' THEN 2 ELSE 3 END,name")
        if name in PREDECESSOR_OBJECTS or name.startswith('listening_summary_')
        or kind == 'trigger' and ('_play_event_calendar_' in name or '_play_event_sources_' in name)]


def _fold_events(conn, prior):
    conn.execute('DROP TABLE play_event_calendar')
    conn.execute('DROP TABLE play_event_sources')
    conn.execute(f'CREATE INDEX {MARKER} ON play_events(day)')
    conn.execute('''CREATE VIEW play_event_calendar AS SELECT p.id event_id,p.day,a.listening_timezone timezone
        FROM play_events p JOIN accounts a ON a.id=p.account_id WHERE p.day IS NOT NULL''')
    conn.execute('''CREATE VIEW play_event_sources AS SELECT p.id event_id,s.value source
        FROM play_events p JOIN json_each(p.sources) s''')
    for event in ('INSERT', 'UPDATE OF sources'):
        action = event.split()[0].lower()
        conn.execute(f'''CREATE TRIGGER exact_play_sources_{action} BEFORE {event} ON play_events
            WHEN EXISTS(SELECT 1 FROM json_each(NEW.sources) GROUP BY value HAVING min(type)<>'text' OR count(*)>1)
            BEGIN SELECT RAISE(ABORT,'Play sources must be unique text values'); END''')
    for name in ('rec_capture_play_events_update', 'rec_evidence_play_events_update'):
        conn.execute(prior[name].replace(' BEGIN ', ' OR OLD.day IS NOT NEW.day '
                                        'OR OLD.sources IS NOT NEW.sources BEGIN ', 1))


def _fold_outputs(conn, prior):
    conn.execute("ALTER TABLE listening_output_publications ADD COLUMN slot TEXT "
                 "CHECK(slot IS NULL OR (status='published' AND slot IN ('current','previous')))")
    conn.execute('UPDATE listening_output_publications SET slot=(SELECT slot FROM listening_output_heads '
                 'WHERE publication_id=listening_output_publications.id) WHERE status=\'published\'')
    conn.execute('CREATE UNIQUE INDEX uq_listening_output_slot ON listening_output_publications(slot) WHERE slot IS NOT NULL')
    conn.execute('''CREATE TRIGGER listening_output_slot BEFORE UPDATE OF slot,status ON listening_output_publications
        WHEN (NEW.status='published')<>(NEW.slot IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'Published listening output requires a slot'); END''')
    for column in ('position INTEGER CHECK(position IS NULL OR (surface=\'listening_chunk\' '
                   "AND typeof(position)='integer' AND position BETWEEN 0 AND 4095))",
                   'from_day TEXT', 'until_day TEXT', '''chunk_bytes INTEGER CHECK(
                    (position IS NULL AND from_day IS NULL AND until_day IS NULL AND chunk_bytes IS NULL)
                    OR (position IS NOT NULL AND from_day IS NOT NULL AND until_day IS NOT NULL
                    AND date(from_day) IS from_day AND date(until_day) IS until_day AND from_day<=until_day
                    AND typeof(chunk_bytes)='integer' AND chunk_bytes BETWEEN 1 AND 1048576))'''):
        conn.execute('ALTER TABLE listening_output_files ADD COLUMN ' + column)
    conn.execute('''UPDATE listening_output_files AS f SET (position,from_day,until_day,chunk_bytes)=
        (SELECT position,from_day,until_day,encoded_bytes FROM listening_output_chunks
         WHERE publication_id=f.publication_id AND chunk_key=f.chunk_key)
        WHERE f.surface='listening_chunk' ''')
    conn.execute('CREATE UNIQUE INDEX uq_listening_output_position ON listening_output_files(publication_id,position) '
                 'WHERE position IS NOT NULL')
    for table in ('listening_output_chunks', 'listening_output_heads'):
        conn.execute('DROP TABLE ' + table)
    conn.execute(prior['listening_output_transition'].replace(
        'FROM listening_output_chunks WHERE publication_id=OLD.id',
        'FROM listening_output_files WHERE publication_id=OLD.id AND position IS NOT NULL').replace(
        " AND NOT EXISTS(SELECT 1 FROM listening_output_heads WHERE publication_id=OLD.id)", ''))
    conn.execute(prior['listening_output_file_update'].replace(
        'OR NEW.built_at IS NOT OLD.built_at', 'OR NEW.built_at IS NOT OLD.built_at '
        'OR NEW.position IS NOT OLD.position OR NEW.from_day IS NOT OLD.from_day '
        'OR NEW.until_day IS NOT OLD.until_day OR NEW.chunk_bytes IS NOT OLD.chunk_bytes', 1))


def install(conn, context):
    prior = definitions(conn, PREDECESSOR_OBJECTS)
    for name, (kind, _) in objects(conn).items():
        if kind == 'trigger' and (name.startswith('listening_summary_')
                or '_play_event_calendar_' in name or '_play_event_sources_' in name
                or name in (*OUTPUT_RETIRED_TRIGGERS, *OUTPUT_CHANGED_TRIGGERS, *CHANGE_TRIGGERS)):
            conn.execute('DROP TRIGGER ' + name)
    play_event_preparation.swap(conn)
    _fold_events(conn, prior)
    conn.execute('DROP TABLE listening_capture_receipts')
    _fold_outputs(conn, prior)
    conn.execute('''INSERT INTO settings(key,value)
        SELECT 'choice_window:'||format,json_object('opportunity_id',opportunity_key,'window',window_text,
               'updated_at',updated_at) FROM listening_choice_windows
        WHERE true ON CONFLICT(key) DO UPDATE SET value=excluded.value''')
    conn.execute('DROP TABLE listening_choice_windows')
    conn.execute('DROP VIEW trend_first_play')
    for table in ('artist_listening_summary', 'recording_listening_summary', 'listening_summary_state'):
        conn.execute('DROP TABLE ' + table)
    for name, sql in listening_summaries.definitions():
        conn.execute(f'CREATE VIEW {name} AS {sql}')
    conn.execute("DELETE FROM settings WHERE key IN ('trend_first_play_source','trend_first_play_watermark',"
                 "'trend_first_play_takeout_seen')")
    for name, sql in recording_library.triggers(album_folded=True, listening_folded=True):
        if name == 'recording_library_play_events_update':
            conn.execute(sql)
    group_preview_work.install_guards(conn, sources={'play_events', 'listening_observations'})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def validate(conn):
    actual = objects(conn, (MARKER, *PROJECTIONS, *REMOVED))
    if (MARKER not in actual or any(actual.get(name, ('',))[0] != 'view' for name in PROJECTIONS)
            or any(name in actual for name in REMOVED)):
        raise MigrationError('Incompatible compact play event storage')


def project_inline_evidence(conn, _context):
    for name, sql in listening_views.inline_event_definitions():
        conn.execute('DROP VIEW ' + name)
        conn.execute(f'CREATE VIEW {name} AS {sql}')


def validate_inline_evidence(conn):
    actual = definitions(conn, EVIDENCE_VIEWS)
    for name, sql in listening_views.inline_event_definitions():
        if tokens(actual.get(name)) != tokens(f'CREATE VIEW {name} AS {sql}'):
            raise MigrationError('Incompatible listening projection: ' + name)
