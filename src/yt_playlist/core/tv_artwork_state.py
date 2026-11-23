"""Current TV artist image results and pending requests."""
import json
from uuid import uuid4

from . import artist_keys, worker_settings


KEY = 'tv_artwork'
RESULTS = 'tv_artist_artwork'
REQUESTS = 'tv_artist_artwork_requests'
COLUMN = "tv_artwork TEXT CHECK(tv_artwork IS NULL OR json_type(tv_artwork) IS 'object')"


def load(conn):
    raw = worker_settings.read(conn, KEY)
    return json.loads(raw) if raw is not None else {'results': {}, 'requests': {}, 'revisions': {}}


def save(conn, state):
    worker_settings.save(conn, KEY, json.dumps(state))


def revision(conn, key):
    return load(conn)['revisions'].get(key)


def read(conn, surface):
    state = load(conn)
    if surface == REQUESTS:
        return {key: row['name'] for key, row in state['requests'].items()} if 'requested_at' in state else None
    if 'published_at' not in state:
        return None
    results = state['results']
    ids = {row['artist_id'] for row in results.values()} - {None}
    marks = ','.join('?' for _ in ids)
    stored = {identity: json.loads(raw) for identity, raw in conn.execute(
        f'SELECT id,tv_artwork FROM artists WHERE id IN ({marks}) AND tv_artwork IS NOT NULL', tuple(ids))}
    return {key: row['value'] if row['artist_id'] is None else stored[row['artist_id']][key]
            for key, row in results.items()}


def _set_artist_value(conn, identity, key, value):
    row = conn.execute('SELECT tv_artwork FROM artists WHERE id=?', (identity,)).fetchone()
    values = json.loads(row[0]) if row and row[0] is not None else {}
    if value is None:
        values.pop(key, None)
    else:
        values[key] = value
    conn.execute('UPDATE artists SET tv_artwork=? WHERE id=?', (json.dumps(values) if values else None, identity))


def add_result(conn, state, key, artist, value):
    result = {'artist_id': artist}
    if artist is None:
        result['value'] = value
    else:
        _set_artist_value(conn, artist, key, value)
    state['results'][key] = result


def _remove(conn, state, section, key):
    row = state[section].pop(key, None)
    if row is None:
        return
    if section == 'results' and row['artist_id'] is not None:
        _set_artist_value(conn, row['artist_id'], key, None)
    if key in state['results'] or key in state['requests']:
        state['revisions'][key] = uuid4().hex
    else:
        state['revisions'].pop(key, None)


def _trim(conn, state, section, limit):
    for key in list(state[section])[:max(0, len(state[section]) - limit)]:
        _remove(conn, state, section, key)


def publish(conn, key, value, at, *, name=None, expected=..., complete=False, limit=60):
    if not conn.in_transaction:
        raise ValueError('TV artwork publication requires its outer transaction')
    state = load(conn)
    if expected is not ... and state['revisions'].get(key) != expected:
        return False
    request = state['requests'].get(key)
    artist = artist_keys.lookup(conn, name or (request['name'] if request else key))
    _remove(conn, state, 'results', key)
    add_result(conn, state, key, artist, value)
    state['published_at'] = at
    state['revisions'][key] = uuid4().hex
    if complete:
        _remove(conn, state, 'requests', key)
    _trim(conn, state, 'results', limit)
    save(conn, state)
    return True


def _add_request(state, key, name, artist, at):
    state['requests'][key] = {'name': name, 'artist_id': artist, 'at': at}
    state['revisions'][key] = uuid4().hex


def request(conn, key, name, at, *, limit=60):
    artist = artist_keys.lookup(conn, name)
    if artist is None:
        return False
    state = load(conn)
    state['requests'].pop(key, None)
    _add_request(state, key, name, artist, at)
    state['requested_at'] = at
    _trim(conn, state, 'requests', limit)
    save(conn, state)
    return True


def replace(conn, surface, values, at):
    state = load(conn)
    if surface == RESULTS:
        _trim(conn, state, 'results', 0)
        state['published_at'] = at
        for key, value in values.items():
            add_result(conn, state, key, artist_keys.lookup(conn, key), value)
            state['revisions'][key] = uuid4().hex
    else:
        _trim(conn, state, 'requests', 0)
        state['requested_at'] = at
        for key, name in values.items():
            artist = artist_keys.lookup(conn, name)
            if artist is None:
                raise ValueError('TV artwork demand requires a known artist')
            _add_request(state, key, name, artist, at)
    save(conn, state)


def clear_results(conn):
    state = load(conn)
    if 'published_at' in state:
        _trim(conn, state, 'results', 0)
        state.pop('published_at')
        save(conn, state)
