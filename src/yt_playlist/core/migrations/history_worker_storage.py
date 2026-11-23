"""Keep retained history checkpoints and prediction IDs with their owners."""
import re

from .. import mode_retention
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from . import artist_storage, prediction_storage
from .track_source_cutover import _replace_table


NAME = 'own_history_worker_state'
PREDICTIONS = 'predictions'
RETENTION = 'taste_mode_retention'
RETIRED = ('prediction_id_insert', 'prediction_id_monotonic')


def predecessor_objects(conn):
    return list(definitions(conn, (PREDICTIONS, RETENTION, *RETIRED)).values())


def install(conn, _context):
    for column in mode_retention.CREDIT_COLUMNS:
        conn.execute(f'ALTER TABLE {RETENTION} ADD COLUMN {column}')
    conn.execute(f'''WITH checkpoint AS (SELECT value FROM settings WHERE key=?)
        UPDATE {RETENTION} SET
            credit_enabled=coalesce((SELECT json_extract(value,'$.enabled') FROM checkpoint),0),
            credit_cursor=coalesce((SELECT json_extract(value,'$.cursor') FROM checkpoint),0),
            credit_completed=coalesce((SELECT json_extract(value,'$.completed') FROM checkpoint),0),
            credit_next_scan_at=coalesce((SELECT json_extract(value,'$.next_scan_at') FROM checkpoint),0)
        WHERE id=1''', (artist_storage.RETENTION_KEY,))

    layout = definitions(conn, (PREDICTIONS,))[PREDICTIONS]
    layout, changed = re.subn(r'\bid\s+INTEGER PRIMARY KEY\b', 'id INTEGER PRIMARY KEY AUTOINCREMENT', layout)
    if changed != 1:
        raise MigrationError('Cannot locate the prediction ID')
    for name in RETIRED:
        conn.execute(f'DROP TRIGGER {name}')
    attached = [sql for sql, in conn.execute("SELECT sql FROM sqlite_schema WHERE tbl_name=? "
        "AND type<>'table' AND sql IS NOT NULL ORDER BY type,name", (PREDICTIONS,))]
    _replace_table(conn, PREDICTIONS, layout)
    for sql in attached:
        conn.execute(sql)
    conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (PREDICTIONS,))
    conn.execute(f'''INSERT INTO sqlite_sequence(name,seq)
        SELECT ?,max(coalesce(max(id),0),coalesce((SELECT CAST(value AS INTEGER) FROM settings WHERE key=?),0))
        FROM {PREDICTIONS}''', (PREDICTIONS, prediction_storage.HIGH_WATER))
    conn.execute('DELETE FROM settings WHERE key IN (?,?)',
                 (artist_storage.RETENTION_KEY, prediction_storage.HIGH_WATER))


def validate(conn):
    found = definitions(conn, (PREDICTIONS, RETENTION, *RETIRED))
    if (set(found) != {PREDICTIONS, RETENTION} or 'autoincrement' not in tokens(found.get(PREDICTIONS))
            or not all(column.split()[0] in tokens(found.get(RETENTION)) for column in mode_retention.CREDIT_COLUMNS)
            or not conn.execute(f'SELECT 1 FROM {RETENTION} WHERE id=1').fetchone()):
        raise MigrationError('Invalid retained history worker state')
