"""Retire the unused copy of the queued Vibe rebuild policy."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'retire_duplicate_vibe_preparation_checkpoint'
TABLE = 'vibe_preparation_checkpoint'


def install(conn, _context):
    # The worker only uses settings.rec_vibe_rebuild_policy to decide whether
    # work is already queued. A checkpoint without that setting proves nothing.
    conn.execute(f'DROP TABLE {TABLE}')


def validate(conn):
    if definitions(conn, (TABLE,)):
        raise MigrationError('The duplicate Vibe preparation checkpoint was not removed')
