"""Move TV request history onto playlists and actions in place."""
from collections import defaultdict
import json

from .. import append_work, collection_recovery, tv_continuations, tv_launch_actions
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_tv_recovery_with_playlists_and_actions'
RETIRED = ('tv_append_intents', 'tv_continuation_receipts', 'tv_launches')
OWNERS = ('playlist_scopes', 'playlists', 'actions')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND tbl_name IN (?,?,?,?,?,?) ORDER BY type,name', (*RETIRED, *OWNERS))]


def _copy_appends(conn):
    grouped = defaultdict(list)
    for row in conn.execute('SELECT * FROM tv_append_intents ORDER BY id'):
        reference = tuple(row[field] for field in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
        captured = collection_recovery.read(*reference, kind='tv-append', owner=row['id'])
        captured.update(request_id=row['request_id'], state=row['state'])
        if 'at' in captured:
            captured['at'] = row['started_at']
        for role in ('before', 'additions', 'items'):
            if role not in captured:
                continue
            videos = json.loads(row[role + '_videos'])
            if role == 'before':
                captured[role] = videos
            else:
                metadata = {item['video_id']: item for item in captured[role]}
                captured[role] = [metadata.get(video, {'video_id': video}) for video in videos]
        grouped[row['playlist_scope_id']].append(dict(id=row['id'], recipe_id=row['recipe_id'],
            stream_token=row['stream_token'], request_id=row['request_id'], state=row['state'],
            started_at=row['started_at'], checkpoint=captured))
    for scope, checkpoints in grouped.items():
        append_work.save(conn, scope, checkpoints)


def _copy_continuations(conn):
    grouped = defaultdict(list)
    for record in conn.execute('SELECT * FROM tv_continuation_receipts ORDER BY id'):
        row = dict(record)
        row['items'] = json.loads(row['items'])
        grouped[row['source_ref']].append(row)
    for membership, rows in grouped.items():
        tv_continuations.save(conn, membership, rows)


def install(conn, _context):
    conn.execute('ALTER TABLE playlist_scopes ADD COLUMN ' + append_work.DECLARATION)
    conn.execute('ALTER TABLE playlists ADD COLUMN ' + tv_continuations.DECLARATION)
    conn.execute('ALTER TABLE actions ADD COLUMN ' + tv_launch_actions.ACCOUNT_COLUMN)
    _copy_appends(conn)
    _copy_continuations(conn)
    for record in conn.execute('SELECT * FROM tv_launches ORDER BY created_at,request_id'):
        state = dict(record)
        state['videos'] = json.loads(state['videos'])
        tv_launch_actions.save(conn, state)
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)
    conn.execute('UPDATE collection_file_writes SET ownership_revision=ownership_revision+1 WHERE id=0')


def validate(conn):
    layout = definitions(conn, (*RETIRED, *OWNERS))
    if (set(layout) != set(OWNERS) or append_work.DECLARATION not in layout['playlist_scopes']
            or tv_continuations.DECLARATION not in layout['playlists']
            or tv_launch_actions.ACCOUNT_COLUMN not in layout['actions']):
        raise MigrationError('Incomplete TV playlist recovery cutover')
