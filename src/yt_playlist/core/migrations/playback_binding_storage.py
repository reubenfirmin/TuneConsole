"""Retain the local player's bounded choice attribution across an upgrade."""
import json

from .. import worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'keep_recent_playback_attribution_in_settings'
RETIRED = 'listening_playback_bindings'
KEY = 'choice_playback_bindings'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name=? AND sql IS NOT NULL '
                                         'ORDER BY type,name', (RETIRED,))]


def install(conn, _context):
    rows = [dict(row) for row in conn.execute(f'SELECT * FROM {RETIRED} '
            'ORDER BY bound_at DESC,account_id,playlist_scope_id,video_id')]
    if rows:
        worker_settings.save(conn, KEY, json.dumps(rows, separators=(',', ':')))
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    if definitions(conn, (RETIRED,)):
        raise MigrationError('Playback attribution still has a dedicated table')
