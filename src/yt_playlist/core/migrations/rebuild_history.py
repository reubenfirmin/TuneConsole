"""Preserve copied rebuild receipts in the existing previous-history setting."""
import json

from ..schema_contract import MigrationError
from ..schema_inspection import objects


NAME = 'retire_database_rebuild_history_tables'
KEY = 'early_adopter_previous_history'
TABLES = ('database_rebuild_source_history', 'database_rebuild_source_changes')


def snapshot(conn):
    return {
        name: [list(row) for row in conn.execute(f'SELECT version,name,applied_at FROM {name} ORDER BY version')]
        for name in objects(conn, TABLES)
    }


def install(conn, _context):
    copied = snapshot(conn)
    if not copied:
        return
    existing = conn.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    try:
        previous = json.loads(existing[0]) if existing else {}
    except (ValueError, TypeError) as error:
        raise MigrationError('Invalid previous migration history') from error
    if not isinstance(previous, dict) or any(key in previous and previous[key] != rows for key, rows in copied.items()):
        raise MigrationError('Conflicting previous migration history')
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (KEY, json.dumps(previous | copied)))
    for name in copied:
        conn.execute(f'DROP TABLE {name}')


def validate(conn):
    if objects(conn, TABLES):
        raise MigrationError('Copied rebuild receipts still occupy separate tables')
