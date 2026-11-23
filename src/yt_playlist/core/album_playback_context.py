"""The pending deliberate album queue and its exact uploaded occurrences."""
import json
from sqlite3 import IntegrityError

from . import albums, collection_recovery, entity_names, playlist_scopes
from .playback_context_policy import pending_album


COLUMNS = ('playback_account_id', 'playback_source_browse_id', 'playback_playlist_scope_id',
           'playback_opportunity_id', 'playback_action_id', 'playback_started_at',
           'playback_video_ids', 'playback_revision')
_ACTIVE = 'playback_revision IS NOT NULL'
_COLUMN_LIST = ','.join(COLUMNS)


@collection_recovery.pinned_read
def load(conn):
    return load_native(conn)


def load_native(conn):
    row = conn.execute(f'SELECT {_COLUMN_LIST},s.provider_playlist_id FROM albums '
        'JOIN playlist_scopes s ON s.id=playback_playlist_scope_id '
        f'WHERE {_ACTIVE}').fetchone()
    if row is None:
        return None
    result = {'opportunity_id': row['playback_opportunity_id'], 'browse': row['playback_source_browse_id'],
              'playlist': row['provider_playlist_id'], 'at': row['playback_started_at']}
    if row['playback_action_id'] is not None:
        result['action_id'] = row['playback_action_id']
    if row['playback_account_id'] is not None:
        result['account_id'] = row['playback_account_id']
    if row['playback_video_ids'] is not None:
        result['videos'] = json.loads(row['playback_video_ids'])
    return result


def put(conn, value):
    value = entity_names.account_document(value)
    value = pending_album(value)
    album = albums.observe(conn, value['browse']) if value is not None else None
    put_native(conn, value, album)


def put_native(conn, value, album):
    with conn.atomic(join=True):
        if value is None:
            _clear(conn)
            return
        conn.execute('UPDATE albums SET playback_revision=playback_revision WHERE 0')
        current = conn.execute(f'SELECT id,playback_revision FROM albums WHERE {_ACTIVE}').fetchone()
        scope = playlist_scopes.ensure(conn, value['playlist'], value.get('account_id'))
        if current and current['id'] != album:
            _clear(conn)
        values = (value.get('account_id'), value['browse'], scope, value['opportunity_id'], value.get('action_id'),
                  value['at'], json.dumps(value['videos']) if 'videos' in value else None,
                  current['playback_revision'] + 1 if current else 1)
        _set(conn, album, values)


def _clear(conn):
    assignments = ','.join(f'{column}=NULL' for column in COLUMNS)
    conn.execute(f'UPDATE albums SET {assignments} WHERE {_ACTIVE}')


def _set(conn, album, values):
    placeholders = ','.join('?' for _ in COLUMNS)
    result = conn.execute(f'UPDATE albums SET ({_COLUMN_LIST})=({placeholders}) WHERE id=?', (*values, album))
    if not result.rowcount:
        raise IntegrityError('Pending playback requires an existing album')


def move_native(conn, retired, target):
    values = conn.execute(f'SELECT {_COLUMN_LIST} FROM albums WHERE id=? AND {_ACTIVE}', (retired,)).fetchone()
    if values is not None:
        _clear(conn)
        _set(conn, target, values)


def bind_native(conn, pending, account_id):
    conn.execute('UPDATE albums SET playback_account_id=?,playback_revision=playback_revision+1 '
        f'WHERE {_ACTIVE} AND playback_account_id IS NULL AND playback_opportunity_id=? '
        'AND playback_started_at=? AND playback_action_id IS ?',
        (account_id, pending['opportunity_id'], pending['at'], pending.get('action_id')))
