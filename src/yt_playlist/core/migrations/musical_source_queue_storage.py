"""Preserve unfinished musical-source work on its existing source row."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'fold_musical_source_queue'
MARKER = 'ix_musical_sources_pending'
RETIRED = 'rec_musical_pending'
TRIGGERS = ('musical_source_insert', 'musical_source_update')
SEQUENCE = 'musical_source_queue_position'
ENQUEUE = f'''UPDATE rec_musical_sources
    SET pending_position=coalesce(pending_position,(SELECT CAST(value AS INTEGER) FROM settings WHERE key='{SEQUENCE}')), pending_cursor=0
    WHERE source_key='''


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND tbl_name IN (?,?) ORDER BY type,name',
        ('rec_musical_sources', RETIRED))]


def install(conn, _context):
    if conn.execute('''
        SELECT 1 FROM rec_musical_pending p LEFT JOIN rec_musical_sources s USING(source_key)
        WHERE s.revision IS NOT p.revision LIMIT 1
    ''').fetchone():
        raise MigrationError('Pending musical work does not match its source revision')
    for name in TRIGGERS:
        conn.execute(f'DROP TRIGGER {name}')
    conn.execute('''ALTER TABLE rec_musical_sources ADD COLUMN pending_position INTEGER
        CHECK(pending_position IS NULL OR (typeof(pending_position)='integer' AND pending_position>=0))''')
    conn.execute('''ALTER TABLE rec_musical_sources ADD COLUMN pending_cursor INTEGER
        CHECK(typeof(revision)='integer' AND revision>=1 AND (
            (pending_position IS NULL AND pending_cursor IS NULL)
            OR (pending_position IS NOT NULL AND typeof(pending_cursor)='integer' AND pending_cursor>=0)))''')
    conn.execute('''UPDATE rec_musical_sources AS s SET pending_position=p.position,pending_cursor=p.cursor
        FROM rec_musical_pending p WHERE s.source_key=p.source_key''')
    conn.execute(f'''CREATE UNIQUE INDEX {MARKER} ON rec_musical_sources(pending_position)
        WHERE pending_position IS NOT NULL''')
    conn.execute('''INSERT INTO settings(key,value) SELECT ?,seq FROM sqlite_sequence
        WHERE name='rec_musical_pending' ''', (SEQUENCE,))
    conn.execute('DROP TABLE rec_musical_pending')
    for name, event in zip(TRIGGERS, ('INSERT', 'UPDATE OF revision'), strict=True):
        conn.execute(f'''CREATE TRIGGER {name} AFTER {event} ON rec_musical_sources BEGIN
            INSERT INTO settings(key,value) SELECT '{SEQUENCE}','1' WHERE NEW.pending_position IS NULL
                ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
            {ENQUEUE}NEW.source_key;
        END''')


def validate(conn):
    if set(definitions(conn, (MARKER, RETIRED))) != {MARKER}:
        raise MigrationError('Musical-source queue was not consolidated')
