"""Typed TV candidates, image work, visual measurements and continuation receipts."""
import json
import math
import re
from . import recommendation_outputs, tv_artwork_state, tv_continuations, worker_settings
from .db_revisions import setting_reads

SURFACES = frozenset({'tv_pool', 'tv_artist_artwork', 'tv_artist_artwork_requests',
                      'tv_visual_checks', 'tv_continuations'})
POOL = ('schema', 'key', 'at', 'items', 'attempted', 'offset', 'state', 'retry_at')
ITEM = ('video_id', 'title', 'artist', 'thumbnail', 'duration', 'kind', 'source_key',
        'source_artist', 'group', 'connection', 'key')
ITEM_SNAPSHOT = ('position', 'source_recording_id', 'source_resolution', 'recording_id', 'video_resolution')
VISUAL = ('verdict', 'version', 'at', 'retry_at', 'difference', 'changed_fraction', 'sample_times')
ARTWORK = ('images', 'retry_at', 'at')
VIDEO = re.compile(r'^[A-Za-z0-9_-]{11}$')
MAX_BYTES = 2 * 1024 * 1024
POOL_KEY = 'tv_prepared_pool'
POOL_PRESENT = 'tv_prepared_pool_present'
VISUAL_KEY = 'tv_visual_checks'


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
    if len({item['video_id'] for item in values}) != len(values):
        raise ValueError('Duplicate TV video')
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
        attempts = _list(value.get('attempted', []), 60)
        for key in attempts:
            _text(key)
        if len(set(attempts)) != len(attempts):
            raise ValueError('Duplicate TV attempt')
    else:
        if len(value) > (240 if surface == 'tv_visual_checks' else 60):
            raise ValueError('TV owner exceeds its row budget')
        for key, item in value.items():
            _text(key)
            if surface == 'tv_artist_artwork_requests':
                _text(item)
            elif surface == 'tv_artist_artwork':
                _mask(item, ARTWORK)
                images = _list(item.get('images', []), 6)
                for image in images:
                    _text(image)
                if len(set(images)) != len(images):
                    raise ValueError('Duplicate TV artwork image')
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
                version, difference, fraction = (item.get(field) for field in ('version', 'difference', 'changed_fraction'))
                if (version is not None and (type(version) is not int or version < 0)
                        or difference is not None and difference < 0
                        or fraction is not None and not 0 <= fraction <= 1):
                    raise ValueError('Invalid TV visual measurement')
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


def _pack_items(conn, values):
    packed = []
    for position, item in enumerate(values):
        source = _recording(conn, item.get('source_key'))
        video = conn.execute('SELECT recording_id FROM track_sources WHERE video_id=?', (item['video_id'],)).fetchone()
        recording = video[0] if video else None
        packed.append(dict(item, position=position, source_recording_id=source,
                           source_resolution=None if source else 'missing_recording', recording_id=recording,
                           video_resolution=None if recording else 'external_video'))
    return packed


def _read_items(raw):
    return [{field: item[field] for field in ITEM if field in item} for item in raw]


def save_pool(conn, pool):
    worker_settings.save(conn, POOL_KEY, json.dumps(pool) if pool is not None else None)
    worker_settings.save(conn, POOL_PRESENT, '1' if pool is not None and len(pool) > 1 else None)


def _write_pool(conn, value, at):
    pool = {**value, 'built_at': at}
    if 'items' in pool:
        pool['items'] = _pack_items(conn, pool['items'])
    if 'attempted' in pool:
        attempts = []
        for position, key in enumerate(pool['attempted']):
            recording = _recording(conn, key)
            attempts.append(dict(position=position, key=key, recording_id=recording,
                                 resolution_reason=None if recording else 'missing_recording'))
        pool['attempted'] = attempts
    save_pool(conn, pool)


def publish_visual(conn, video, value, at, *, limit=240):
    validate('tv_visual_checks', {video: value})
    if not conn.in_transaction:
        raise ValueError('TV visual publication requires its outer transaction')
    results = read(conn, 'tv_visual_checks') or {}
    results.pop(video, None)
    results[video] = value
    _write_visuals(conn, dict(list(results.items())[-limit:]), at)


def _write_visuals(conn, results, at):
    worker_settings.save(conn, VISUAL_KEY, json.dumps({'built_at': at, 'results': results}))


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


def complete_receipt(conn, key, value, at):
    validate('tv_continuations', {key: value})
    if not conn.in_transaction:
        raise ValueError('TV continuation completion requires its outer transaction')
    address = _address(key)
    previous = tv_continuations.header(conn, key, address[0])
    if previous and previous['status'] == 'complete':
        raise ValueError('TV receipt has already completed')
    if previous and previous['target_stream'] != value['stream']:
        raise ValueError('TV rollover completion belongs to another intent')
    write_receipt(conn, key, address, value, at, previous, 60)


def write_receipt(conn, key, address, value, at, previous, limit):
    tv_continuations.complete(conn, key, address, value, at, _pack_items(conn, value.get('items', [])), limit, previous)


def receipt(conn, key):
    return _receipt_value(tv_continuations.header(conn, key, _address(key)[0]))


def _receipt_value(row):
    if row is None:
        return None
    if row['status'] == 'pending':
        return {'pending': True, 'stream': row['target_stream']}
    return {'playlist_id': row['target_playlist_id'], 'stream': row['target_stream'], 'items': _read_items(row['items'])}


def write(conn, surface, value, at):
    if not conn.in_transaction:
        raise ValueError('TV state requires its outer transaction')
    validate(surface, value)
    _number(at)
    if surface == 'tv_pool':
        _write_pool(conn, value, at)
    elif surface in (tv_artwork_state.RESULTS, tv_artwork_state.REQUESTS):
        tv_artwork_state.replace(conn, surface, value, at)
    elif surface == 'tv_visual_checks':
        _write_visuals(conn, value, at)
    else:
        tv_continuations.clear(conn)
        for key, item in value.items():
            complete_receipt(conn, key, item, at)


def read(conn, surface):
    if surface == 'tv_pool':
        raw = worker_settings.read(conn, POOL_KEY)
        if raw is None:
            return None
        result = json.loads(raw)
        result.pop('built_at')
        if 'items' in result:
            result['items'] = _read_items(result['items'])
        if 'attempted' in result:
            result['attempted'] = [attempt['key'] for attempt in result['attempted']]
        return result
    if surface in (tv_artwork_state.RESULTS, tv_artwork_state.REQUESTS):
        return tv_artwork_state.read(conn, surface)
    if surface == 'tv_visual_checks':
        raw = worker_settings.read(conn, VISUAL_KEY)
        return json.loads(raw)['results'] if raw is not None else None
    if surface == 'tv_continuations':
        return {row['request_key']: _receipt_value(row) for row in tv_continuations.read_all(conn)}
    raise ValueError('Unknown TV owner')


def present(conn, surface):
    if surface == 'tv_pool':
        with setting_reads(conn, (POOL_PRESENT,)):
            return conn.execute('SELECT 1 FROM settings WHERE key=?', (POOL_PRESENT,)).fetchone() is not None
    if surface in (tv_artwork_state.RESULTS, tv_artwork_state.REQUESTS):
        section = 'results' if surface == tv_artwork_state.RESULTS else 'requests'
        return bool(tv_artwork_state.load(conn)[section])
    if surface == 'tv_continuations':
        return tv_continuations.present(conn)
    if surface == VISUAL_KEY:
        with setting_reads(conn, (VISUAL_KEY,)):
            return conn.execute("SELECT 1 FROM settings WHERE key=? AND value->>'results'<>'{}'",
                                (VISUAL_KEY,)).fetchone() is not None
    raise ValueError('Unknown TV owner')
