"""Keep queue positions and impression IDs with the rows they order."""
import re

from .. import musical_source_queue
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from .musical_source_queue_storage import SEQUENCE, TRIGGERS
from .served_impression_storage import NEXT_ID_KEY
from .track_source_cutover import _replace_table


NAME = 'own_work_sequences'
IMPRESSIONS = 'rec_served_impressions'
SOURCES = 'rec_musical_sources'
OWNERS = (IMPRESSIONS, SOURCES)
POSITION_INDEX = 'ix_musical_source_last_position'


def predecessor_objects(conn):
    names = (*OWNERS, *TRIGGERS)
    schema = definitions(conn, names)
    return [schema[name] for name in names]


def install(conn, _context):
    conn.execute(f'''ALTER TABLE {SOURCES} ADD COLUMN last_queue_position INTEGER
        CHECK(last_queue_position IS NULL OR (typeof(last_queue_position)='integer' AND last_queue_position>0))''')
    conn.execute(f'UPDATE {SOURCES} SET last_queue_position=nullif(pending_position,0) '
                 'WHERE pending_position IS NOT NULL')
    conn.execute(f'''UPDATE {SOURCES} SET last_queue_position=(SELECT CAST(value AS INTEGER) FROM settings WHERE key=?)
        WHERE source_key=(SELECT source_key FROM {SOURCES} ORDER BY last_queue_position DESC,source_key LIMIT 1)
        AND coalesce(last_queue_position,0)<(SELECT CAST(value AS INTEGER) FROM settings WHERE key=?)''',
        (SEQUENCE, SEQUENCE))
    conn.execute(f'CREATE INDEX {POSITION_INDEX} ON {SOURCES}(last_queue_position)')
    for name, event in zip(TRIGGERS, ('INSERT', 'UPDATE OF revision'), strict=True):
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(f'''CREATE TRIGGER {name} AFTER {event} ON {SOURCES} BEGIN
            {musical_source_queue.ENQUEUE}NEW.source_key;
        END''')

    layout = definitions(conn, (IMPRESSIONS,))[IMPRESSIONS]
    layout, changed = re.subn(r'\bid\s+INTEGER PRIMARY KEY\b', 'id INTEGER PRIMARY KEY AUTOINCREMENT', layout)
    if changed != 1:
        raise MigrationError('Cannot locate the served impression ID')
    attached = [sql for sql, in conn.execute("SELECT sql FROM sqlite_schema WHERE tbl_name=? "
                                             "AND type<>'table' AND sql IS NOT NULL ORDER BY type,name", (IMPRESSIONS,))]
    _replace_table(conn, IMPRESSIONS, layout)
    for sql in attached:
        conn.execute(sql)
    conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (IMPRESSIONS,))
    conn.execute(f"""
        INSERT INTO sqlite_sequence(name,seq)
        SELECT ?,max(coalesce(max(id),0),coalesce((SELECT CAST(value AS INTEGER)-1 FROM settings WHERE key=?),0))
        FROM {IMPRESSIONS}
    """, (IMPRESSIONS, NEXT_ID_KEY))
    conn.execute('DELETE FROM settings WHERE key IN (?,?)', (SEQUENCE, NEXT_ID_KEY))


def validate(conn):
    names = (*OWNERS, POSITION_INDEX)
    schema = definitions(conn, names)
    if set(schema) != set(names) or 'autoincrement' not in tokens(schema[IMPRESSIONS]):
        raise MigrationError('Worker sequences are missing')
