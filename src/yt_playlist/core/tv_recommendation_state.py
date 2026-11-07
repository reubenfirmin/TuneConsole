"""Typed TV candidates, image work, visual measurements and continuation receipts."""
import math
import re

from . import artists, recommendation_outputs
from .tv_output_retention import clear_disposable

SURFACES = frozenset({'tv_pool', 'tv_artist_artwork', 'tv_artist_artwork_requests',
                      'tv_visual_checks', 'tv_continuations'})
POOL = ('schema', 'key', 'at', 'items', 'attempted', 'offset', 'state', 'retry_at')
ITEM = ('video_id', 'title', 'artist', 'thumbnail', 'duration', 'kind', 'source_key',
        'source_artist', 'group', 'connection', 'key')
VISUAL = ('verdict', 'version', 'at', 'retry_at', 'difference', 'changed_fraction', 'sample_times')
ARTWORK = ('images', 'retry_at', 'at')
VIDEO = re.compile(r'^[A-Za-z0-9_-]{11}$')
MAX_BYTES = 2 * 1024 * 1024


def _mask(value, fields):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError('Unsupported TV state fields')
    return sum(1 << index for index, field in enumerate(fields) if field in value)


def _view(mask, fields, values):
    return {field: value for index, (field, value) in enumerate(zip(fields, values)) if mask & (1 << index)}


def _number(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('Invalid TV measurement')
    return value


def _text(value, *, nullable=False):
    if value is None and nullable:
        return value
    if not isinstance(value, str) or (not value and not nullable) or len(value.encode()) > 4096:
        raise ValueError('Invalid TV identity or display text')
    return value


def _list(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('TV relation exceeds its budget')
    return value


def _items(values):
    for item in _list(values, 60):
        _mask(item, ITEM)
        if not isinstance(item.get('video_id'), str) or not VIDEO.fullmatch(item['video_id']):
            raise ValueError('Invalid TV provider video identity')
        if item.get('key') is not None and item['key'] != item['video_id']:
            raise ValueError('TV playback key does not match its video')
        for field in ITEM:
            if field != 'duration':
                _text(item.get(field), nullable=True)
        duration = item.get('duration')
        if duration is not None and (type(duration) is not int or duration <= 0):
            raise ValueError('Invalid TV duration')
        if item.get('group') is not None and item['group'] not in ('liked', 'your_artists', 'discovery'):
            raise ValueError('Invalid TV source group')
    return values


def decode(surface, raw):
    if not isinstance(raw, str) or not 0 < len(raw.encode()) <= MAX_BYTES:
        raise ValueError('TV state exceeds its byte budget')
    return validate(surface, recommendation_outputs.decode_json(raw))


def validate(surface, value):
    if surface not in SURFACES or not isinstance(value, dict):
        raise ValueError('Unknown TV owner or state shape')
    if surface == 'tv_pool':
        _mask(value, POOL)
        for field in ('at', 'retry_at'):
            _number(value.get(field))
        for field in ('schema', 'offset'):
            if value.get(field) is not None and (type(value[field]) is not int or value[field] < 0):
                raise ValueError('Invalid TV pool version or cursor')
        _text(value.get('key'), nullable=True)
        if value.get('state') is not None and value['state'] not in ('searching', 'exhausted', 'no_sources'):
            raise ValueError('Invalid TV preparation state')
        _items(value.get('items', []))
        for key in _list(value.get('attempted', []), 60):
            _text(key)
    else:
        if len(value) > (240 if surface == 'tv_visual_checks' else 60):
            raise ValueError('TV owner exceeds its row budget')
        for key, item in value.items():
            _text(key)
            if surface == 'tv_artist_artwork_requests':
                _text(item)
            elif surface == 'tv_artist_artwork':
                _mask(item, ARTWORK)
                for image in _list(item.get('images', []), 6):
                    _text(image)
                _number(item.get('retry_at'))
                _number(item.get('at'))
            elif surface == 'tv_visual_checks':
                if not VIDEO.fullmatch(key):
                    raise ValueError('Invalid measured provider video')
                _mask(item, VISUAL)
                if item.get('verdict') is not None and item['verdict'] not in ('static', 'moving', 'unknown'):
                    raise ValueError('Invalid TV visual verdict')
                for field in ('version', 'at', 'retry_at', 'difference', 'changed_fraction'):
                    _number(item.get(field))
                for at in _list(item.get('sample_times', []), 2):
                    if _number(at) is None or at < 0:
                        raise ValueError('Invalid visual sample time')
            else:
                _mask(item, ('playlist_id', 'stream', 'items'))
                if type(item.get('playlist_id')) is not int or item['playlist_id'] <= 0:
                    raise ValueError('Invalid TV continuation playlist')
                _text(item.get('stream'))
                _items(item.get('items', []))
    return value


def _recording(conn, key):
    row = conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
    return row[0] if row else None


def _artist(conn, value):
    return artists.lookup(conn, value) if value else None


def _insert_items(conn, table, owner_column, identity, values):
    columns = ','.join('source_group' if field == 'group' else field for field in ITEM)
    for position, item in enumerate(values):
        source = _recording(conn, item.get('source_key'))
        video = conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?', (item['video_id'],)).fetchone()
        recording = video[0] if video else None
        conn.execute(f'INSERT INTO {table}({owner_column},position,field_mask,{columns},'
            'source_recording_id,source_resolution,recording_id,video_resolution) VALUES (' + ','.join('?' for _ in range(7 + len(ITEM))) + ')',
            (identity, position, _mask(item, ITEM), *(item.get(field) for field in ITEM), source,
             None if source else 'missing_recording', recording, None if recording else 'external_video'))


def _read_items(conn, table, owner_column, identity):
    columns = ','.join('source_group' if field == 'group' else field for field in ITEM)
    return [_view(row[0], ITEM, row[1:]) for row in conn.execute(
        f'SELECT field_mask,{columns} FROM {table} WHERE {owner_column}=? ORDER BY position', (identity,))]


def _state(conn, table, at):
    conn.execute(f'INSERT INTO {table} VALUES (1,?) ON CONFLICT(id) DO UPDATE SET built_at=excluded.built_at', (at,))


def _write_pool(conn, value, at):
    conn.execute('DELETE FROM tv_pool_items')
    conn.execute('DELETE FROM tv_pool_attempts')
    conn.execute('DELETE FROM tv_pool_state')
    conn.execute('INSERT INTO tv_pool_state VALUES (1,?,?,?,?,?,?,?,?)',
        (at, _mask(value, POOL), value.get('schema'), value.get('key'), value.get('at'),
         value.get('offset'), value.get('state'), value.get('retry_at')))
    _insert_items(conn, 'tv_pool_items', 'pool_id', 1, value.get('items', []))
    for position, key in enumerate(value.get('attempted', [])):
        recording = _recording(conn, key)
        conn.execute('INSERT INTO tv_pool_attempts VALUES (1,?,?,?,?)',
            (position, key, recording, None if recording else 'missing_recording'))


def publish_artwork(conn, key, value, at, *, expected_request=None, expected_artwork=..., complete=False):
    """Merge one artist; acknowledge only the exact demand this work observed."""
    validate('tv_artist_artwork', {key: value})
    if not conn.in_transaction:
        raise ValueError('TV artwork publication requires its outer transaction')
    if expected_request is not None and not conn.execute(
            'SELECT 1 FROM tv_artist_requests WHERE artist_key=? AND id=?', (key, expected_request)).fetchone():
        return False
    if expected_artwork is not ...:
        row = conn.execute('SELECT id FROM tv_artist_artwork WHERE artist_key=?', (key,)).fetchone()
        if (row[0] if row else None) != expected_artwork:
            return False
    _state(conn, 'tv_artwork_state', at)
    artist = _artist(conn, key)
    conn.execute('DELETE FROM tv_artist_artwork WHERE artist_key=?', (key,))
    identity = conn.execute('INSERT INTO tv_artist_artwork(artist_key,artist_id,resolution_reason,field_mask,retry_at,at) '
        'VALUES (?,?,?,?,?,?) RETURNING id',
        (key, artist, None if artist else 'missing_artist', _mask(value, ARTWORK), value.get('retry_at'), value.get('at'))).fetchone()[0]
    conn.executemany('INSERT INTO tv_artist_images VALUES (?,?,?)',
                     [(identity, position, url) for position, url in enumerate(value.get('images', []))])
    conn.execute('DELETE FROM tv_artist_artwork WHERE id NOT IN (SELECT id FROM tv_artist_artwork ORDER BY id DESC LIMIT 60)')
    if complete and expected_request is not None:
        conn.execute('DELETE FROM tv_artist_requests WHERE artist_key=? AND id=?', (key, expected_request))
    return True


def request_artwork(conn, key, name, at):
    _state(conn, 'tv_artwork_request_state', at)
    artist = _artist(conn, name)
    if artist is None:
        return False
    conn.execute('INSERT INTO tv_artist_requests(artist_key,display_name,artist_id,requested_at) VALUES (?,?,?,?) '
        'ON CONFLICT(artist_key) DO NOTHING', (key, name, artist, at))
    conn.execute('DELETE FROM tv_artist_requests WHERE id NOT IN (SELECT id FROM tv_artist_requests ORDER BY id DESC LIMIT 60)')
    return True


def publish_visual(conn, video, value, at):
    validate('tv_visual_checks', {video: value})
    if not conn.in_transaction:
        raise ValueError('TV visual publication requires its outer transaction')
    _state(conn, 'tv_visual_state', at)
    conn.execute('DELETE FROM tv_visual_results WHERE video_id=?', (video,))
    fields = VISUAL[:-1]
    identity = conn.execute('INSERT INTO tv_visual_results(video_id,field_mask,' + ','.join(fields) + ') '
        'VALUES (' + ','.join('?' for _ in range(8)) + ') RETURNING id',
        (video, _mask(value, VISUAL), *(value.get(field) for field in fields))).fetchone()[0]
    conn.executemany('INSERT INTO tv_visual_samples VALUES (?,?,?)',
                     [(identity, position, moment) for position, moment in enumerate(value.get('sample_times', []))])
    conn.execute('DELETE FROM tv_visual_results WHERE id NOT IN (SELECT id FROM tv_visual_results ORDER BY id DESC LIMIT 240)')


def _playlist(conn, identity):
    row = conn.execute('SELECT id FROM playlist_membership_refs WHERE id=?', (identity,)).fetchone()
    return row[0] if row else None


def _address(key):
    try:
        source, stream, request = key.split(':', 2)
        source = int(source)
    except (ValueError, AttributeError):
        raise ValueError('Invalid TV receipt address') from None
    # A saved playlist has no active stream token. Its empty input token is
    # still part of the exact request address; the new target token is required.
    if source <= 0 or not request:
        raise ValueError('Invalid TV receipt address')
    return source, stream, request


def begin_rollover(conn, key, stream, at):
    """Persist intent before a provider create, which has no idempotency key."""
    if not conn.in_transaction:
        raise ValueError('TV rollover admission requires its outer transaction')
    source, input_stream, request = _address(key)
    if conn.execute('SELECT 1 FROM tv_continuation_receipts WHERE request_key=?', (key,)).fetchone():
        return False
    if conn.execute("SELECT count(*) FROM tv_continuation_receipts WHERE status='pending'").fetchone()[0] >= 60:
        raise ValueError('Too many unconfirmed TV playlist creations')
    resolved = _playlist(conn, source)
    conn.execute('INSERT INTO tv_continuation_receipts(request_key,source_playlist_id,source_ref,'
        'source_resolution,input_stream,request_id,status,target_stream,created_at) VALUES (?,?,?,?,?,?,\'pending\',?,?)',
        (key, source, resolved, None if resolved else 'missing_playlist', input_stream, request, stream, at))
    return True


def complete_receipt(conn, key, value, at):
    validate('tv_continuations', {key: value})
    if not conn.in_transaction:
        raise ValueError('TV continuation completion requires its outer transaction')
    source, stream, request = _address(key)
    previous = conn.execute('SELECT id,status,target_stream FROM tv_continuation_receipts WHERE request_key=?', (key,)).fetchone()
    if previous and previous[1] == 'complete':
        raise ValueError('TV receipt has already completed')
    if previous and previous[2] != value['stream']:
        raise ValueError('TV rollover completion belongs to another intent')
    source_ref = _playlist(conn, source)
    target = _playlist(conn, value['playlist_id'])
    if previous:
        identity = previous[0]
        conn.execute("UPDATE tv_continuation_receipts SET status='complete',target_playlist_id=?,target_ref=?,"
            'target_resolution=?,built_at=? WHERE id=?',
            (value['playlist_id'], target, None if target else 'missing_playlist', at, identity))
    else:
        identity = conn.execute('INSERT INTO tv_continuation_receipts(request_key,source_playlist_id,source_ref,'
            'source_resolution,input_stream,request_id,status,target_playlist_id,target_ref,target_resolution,target_stream,built_at) '
            "VALUES (?,?,?,?,?,?,'complete',?,?,?,?,?) RETURNING id",
            (key, source, source_ref, None if source_ref else 'missing_playlist', stream, request,
             value['playlist_id'], target, None if target else 'missing_playlist', value['stream'], at)).fetchone()[0]
    _insert_items(conn, 'tv_continuation_items', 'receipt_id', identity, value.get('items', []))
    conn.execute("DELETE FROM tv_continuation_receipts WHERE status='complete' AND id NOT IN "
                 "(SELECT id FROM tv_continuation_receipts WHERE status='complete' ORDER BY id DESC LIMIT 60)")


def receipt(conn, key):
    row = conn.execute('SELECT id,status,target_playlist_id,target_stream FROM tv_continuation_receipts WHERE request_key=?', (key,)).fetchone()
    if row is None:
        return None
    if row[1] == 'pending':
        return {'pending': True, 'stream': row[3]}
    return {'playlist_id': row[2], 'stream': row[3],
            'items': _read_items(conn, 'tv_continuation_items', 'receipt_id', row[0])}


def write(conn, surface, value, at, *, historical=False):
    if not conn.in_transaction:
        raise ValueError('TV state requires its outer transaction')
    validate(surface, value)
    _number(at)
    if surface == 'tv_pool':
        _write_pool(conn, value, at)
    elif surface == 'tv_artist_artwork':
        conn.execute('DELETE FROM tv_artist_artwork')
        _state(conn, 'tv_artwork_state', at)
        for key, item in value.items():
            publish_artwork(conn, key, item, at)
    elif surface == 'tv_artist_artwork_requests':
        conn.execute('DELETE FROM tv_artist_requests')
        _state(conn, 'tv_artwork_request_state', at)
        for key, name in value.items():
            artist = _artist(conn, name)
            if artist is None and not historical:
                raise ValueError('TV artwork demand requires a known artist')
            conn.execute('INSERT INTO tv_artist_requests(artist_key,display_name,artist_id,requested_at,resolution_reason) '
                'VALUES (?,?,?,?,?)', (key, name, artist, None if historical else at, None if artist else 'missing_artist'))
    elif surface == 'tv_visual_checks':
        conn.execute('DELETE FROM tv_visual_results')
        _state(conn, 'tv_visual_state', at)
        for key, item in value.items():
            publish_visual(conn, key, item, at)
    else:
        if conn.execute("SELECT 1 FROM tv_continuation_receipts WHERE status='pending'").fetchone():
            raise ValueError('Cannot replace unconfirmed TV creations')
        conn.execute('DELETE FROM tv_continuation_receipts')
        for key, item in value.items():
            complete_receipt(conn, key, item, at)


def read(conn, surface):
    if surface == 'tv_pool':
        row = conn.execute('SELECT field_mask,schema_version,cache_key,at,offset,state,retry_at FROM tv_pool_state WHERE id=1').fetchone()
        if row is None:
            return None
        result = _view(row[0], ('schema', 'key', 'at', 'items', 'attempted', 'offset', 'state', 'retry_at'),
                       (*row[1:4], None, None, *row[4:]))
        if row[0] & (1 << POOL.index('items')):
            result['items'] = _read_items(conn, 'tv_pool_items', 'pool_id', 1)
        if row[0] & (1 << POOL.index('attempted')):
            result['attempted'] = [entry[0] for entry in conn.execute('SELECT raw_key FROM tv_pool_attempts ORDER BY position')]
        return result
    if surface == 'tv_artist_artwork':
        if not conn.execute('SELECT 1 FROM tv_artwork_state').fetchone():
            return None
        result = {}
        for identity, key, mask, retry, at in conn.execute('SELECT id,artist_key,field_mask,retry_at,at FROM tv_artist_artwork ORDER BY id'):
            images = [row[0] for row in conn.execute('SELECT url FROM tv_artist_images WHERE artwork_id=? ORDER BY position', (identity,))]
            result[key] = _view(mask, ARTWORK, (images, retry, at))
        return result
    if surface == 'tv_artist_artwork_requests':
        if not conn.execute('SELECT 1 FROM tv_artwork_request_state').fetchone():
            return None
        return dict(conn.execute('SELECT artist_key,display_name FROM tv_artist_requests ORDER BY id'))
    if surface == 'tv_visual_checks':
        if not conn.execute('SELECT 1 FROM tv_visual_state').fetchone():
            return None
        result = {}
        for row in conn.execute('SELECT id,video_id,field_mask,' + ','.join(VISUAL[:-1]) + ' FROM tv_visual_results ORDER BY id'):
            samples = [entry[0] for entry in conn.execute('SELECT at FROM tv_visual_samples WHERE result_id=? ORDER BY position', (row[0],))]
            result[row[1]] = _view(row[2], VISUAL, (*row[3:], samples))
        return result
    if surface == 'tv_continuations':
        return {key: receipt(conn, key) for key, in conn.execute(
            "SELECT request_key FROM tv_continuation_receipts WHERE status='complete' ORDER BY id")}
    raise ValueError('Unknown TV owner')
