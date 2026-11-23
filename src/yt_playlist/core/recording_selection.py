"""Recording facts needed before a mix chooses its playable uploads."""
import sqlite3

from .db_writes import atomic_write
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions
from .worker_settings import read, save


NAME = 'recording_mix_selection'
READY = 'recording_mix_selection_ready'
COLUMNS = {
    'effective_album': 'TEXT',
    'effective_duration_s': 'INTEGER',
    'effective_mb_year': 'TEXT',
    'has_playable_source': 'INTEGER NOT NULL DEFAULT 0',
}
PLAYABLE_SOURCE = 's.video_id IS NOT NULL AND s.effective_available IS NOT 0'
SOURCE_PREFERENCE = 's.library_origin IS NULL,s.id'
PREFERRED_SOURCE_ID = 'COALESCE(MIN(s.id) FILTER (WHERE s.library_origin IS NOT NULL),MIN(s.id))'


def _refresh(recordings):
    fields = ','.join(COLUMNS)
    return f'''UPDATE recordings SET ({fields})=(
        SELECT s.effective_album,s.effective_duration_s,s.effective_mb_year,s.id IS NOT NULL
        FROM (SELECT 1) LEFT JOIN track_sources s ON s.id=(
            SELECT s.id FROM track_sources s WHERE s.recording_id=recordings.id AND {PLAYABLE_SOURCE}
            ORDER BY {SOURCE_PREFERENCE} LIMIT 1
        )
    ) WHERE id IN ({recordings});'''


def _triggers():
    watched = ('recording_id', 'video_id', 'library_origin', 'effective_available',
               'effective_album', 'effective_duration_s', 'effective_mb_year')
    changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in watched)
    for event, target, when in (
        ('INSERT', 'NEW.recording_id', 'WHEN NEW.video_id IS NOT NULL'),
        ('DELETE', 'OLD.recording_id', 'WHEN OLD.video_id IS NOT NULL'),
        ('UPDATE OF ' + ','.join(watched), 'OLD.recording_id,NEW.recording_id',
         f'WHEN (NEW.video_id IS NOT NULL OR OLD.video_id IS NOT NULL) AND ({changed})'),
    ):
        name = 'recording_mix_selection_' + event.split()[0].lower()
        yield name, f'CREATE TRIGGER {name} AFTER {event} ON track_sources {when} BEGIN {_refresh(target)} END'


def installed(conn):
    return bool(definitions(conn, ('recording_mix_selection_update',)))


def install(conn, _context):
    for field, declaration in COLUMNS.items():
        conn.execute(f'ALTER TABLE recordings ADD COLUMN {field} {declaration}')
    for _, sql in _triggers():
        conn.execute(sql)


def validate(conn):
    expected = dict(_triggers())
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if name not in actual or tokens(actual[name]) != tokens(sql):
            raise MigrationError('Incompatible recording mix selection: ' + name)


def ready(conn):
    return read(conn, READY) == '1'


def prepare(store):
    """Fill existing rows once. Interruption rolls back the fill and receipt together."""
    with store._lock:
        if ready(store.conn):
            return True
    try:
        with atomic_write(store, wait_seconds=0):
            conn = store.conn
            if not ready(conn):
                conn.execute(_refresh('SELECT id FROM recordings'))
                save(conn, READY, 1)
    except sqlite3.OperationalError as error:
        if getattr(error, 'sqlite_errorcode', 0) & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        return False
    return True
