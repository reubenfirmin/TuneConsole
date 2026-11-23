"""Persistence for the dated listener replay, its last publication and choice input."""
import json

from .rec_model_policy import CHOICE_SUPPORT_SURFACE, PERMANENT_SURFACE, PERMANENT_WORK


SURFACES = (PERMANENT_SURFACE, PERMANENT_WORK, CHOICE_SUPPORT_SURFACE)
TABLE = 'listener_model_documents'
SQL = f"""CREATE TABLE {TABLE} (
    surface TEXT PRIMARY KEY CHECK(surface IN {SURFACES}),
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload)='object'),
    updated_at REAL NOT NULL)"""


def install(conn, context):
    conn.execute(SQL)


def validate(conn):
    from .schema_contract import MigrationError, tokens

    row = conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?", (TABLE,)).fetchone()
    if row is None or tokens(row[0]) != tokens(SQL):
        raise MigrationError('Incompatible listener model document owner')


def encoded(conn, surface):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (surface,)).fetchone()
    return json.loads(row[0])['payload'] if row else None


def present(conn, surface):
    return conn.execute('SELECT 1 FROM settings WHERE key=?', (surface,)).fetchone() is not None


def write(conn, surface, raw, now):
    if surface not in SURFACES or not isinstance(json.loads(raw), dict):
        raise ValueError('Invalid listener model document')
    value = json.dumps({'payload': raw, 'updated_at': now}, allow_nan=False)
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (surface, value))
