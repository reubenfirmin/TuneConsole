"""Read retained Radio publications without loading their preparation workers."""
import hashlib
from pathlib import Path

from . import fao
from .schema_contract import MigrationError


def session_selected(conn):
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='radio_sessions'").fetchone():
        return None
    if not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_radio_head'").fetchone():
        return None
    row = conn.execute("SELECT revision FROM published_radio_head WHERE name='radio-session' AND active=1").fetchone()
    return row[0] if row else None


def session_value(conn, revision, default=None):
    row = conn.execute('SELECT * FROM prepared_radio_sessions WHERE generation=?', (revision,)).fetchone()
    if row is None:
        raise MigrationError('Published Radio checkpoint is missing')
    if not row['source_present']:
        return default
    if row['filename'] is None:
        return None
    with fao.read(row['filename']) as stream:
        encoded = stream.read(row['byte_size'] + 1)
    if (len(encoded) != row['byte_size'] or hashlib.sha256(encoded).hexdigest() != row['digest']
            or Path(row['filename']).stem != row['digest']):
        raise MigrationError('Prepared Radio file does not match its digest')
    return encoded.decode('utf-8')
