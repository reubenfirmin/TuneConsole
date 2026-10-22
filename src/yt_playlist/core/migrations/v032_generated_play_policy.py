"""Enforce the generated-play constraint omitted by v001's optional-column copy.

PRAGMA table_info cannot retain CHECK declarations. Repair installed databases
through a new version, preserving every event and its unknown/ordinary/generated
policy. Replacing just the column leaves event IDs and all dependent ledgers intact.
"""
from . import v026_listening_observations as listening
from .runner import MigrationError


def apply(conn, context):
    if conn.execute('SELECT 1 FROM play_events WHERE is_generated IS NOT NULL '
                    'AND is_generated NOT IN (0,1) LIMIT 1').fetchone():
        raise MigrationError('Invalid generated-play policy; original values retained for recovery')
    # Remove dependent projections while SQLite replaces the column; the entire
    # change is invisible until the runner commits, and rolls back on any failure.
    for sql in reversed(listening.PROJECTIONS):
        conn.execute(f'DROP VIEW {sql.split()[2]}')
    conn.execute('ALTER TABLE play_events ADD COLUMN migration_generated_policy INTEGER '
                 'CHECK(migration_generated_policy IN (0,1))')
    conn.execute('UPDATE play_events SET migration_generated_policy=is_generated')
    conn.execute('ALTER TABLE play_events DROP COLUMN is_generated')
    conn.execute('ALTER TABLE play_events RENAME COLUMN migration_generated_policy TO is_generated')
    for sql in listening.PROJECTIONS:
        conn.execute(sql)
