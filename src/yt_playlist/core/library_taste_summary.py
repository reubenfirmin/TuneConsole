"""Current publication of library-wide listening facets for Home."""
import json

from .db_revisions import setting_reads
from .schema_contract import MigrationError, tokens


KEY = 'library_taste_summary'
SQL = """CREATE TABLE library_taste_summary (
    id INTEGER PRIMARY KEY CHECK(id=1),
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload)='object'),
    built_at REAL NOT NULL)"""


def install(conn, context):
    conn.execute(SQL)


def validate(conn):
    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' "
                       "AND name='library_taste_summary'").fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible library taste summary owner')


def read(conn):
    with setting_reads(conn, (KEY,)):
        row = conn.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    return json.loads(json.loads(row[0])['payload']) if row else None


def present(conn):
    with setting_reads(conn, (KEY,)):
        return conn.execute('SELECT 1 FROM settings WHERE key=?', (KEY,)).fetchone() is not None


def publish(conn, summary, now):
    if not isinstance(summary, dict):
        raise ValueError('Library taste summary must be a JSON object')
    value = json.dumps({'payload': json.dumps(summary, allow_nan=False), 'built_at': now}, allow_nan=False)
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (KEY, value))


def clear(conn):
    conn.execute('DELETE FROM settings WHERE key=?', (KEY,))
