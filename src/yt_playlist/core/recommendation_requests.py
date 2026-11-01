"""Typed Home demand and held listening windows, separate from prepared output."""
from . import album_note_requests, genre_identity
from .recommendation_work import finite

FORMATS = ('albums', 'mixes', 'radio')
SURFACES = ('album_menu_refresh', 'home_genre_request', *(f'choice_window:{format}' for format in FORMATS))


def _integer(value):
    if type(value) is not int or not 0 <= value < 2 ** 63:
        raise ValueError('Expected a nonnegative integer demand epoch')
    return value


def _strings(value, maximum=100):
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) or not item or len(item.encode()) > 1024 for item in value)):
        raise ValueError('Expected a bounded list of nonempty demand identifiers')
    return value


def parse(surface, value, at):
    if at is not None:
        finite(at)
    if surface not in SURFACES or not isinstance(value, dict):
        raise ValueError('Unknown recommendation demand owner or shape')
    if not value:
        return value
    if surface.startswith('choice_window:'):
        if set(value) != {'opportunity_id', 'window'}:
            raise ValueError('Unclassified listening window fields')
        _strings([value['opportunity_id']])
        if (len(value['opportunity_id'].encode()) > 64 or not isinstance(value['window'], str)
                or len(value['window'].encode()) > 1024):
            raise ValueError('Invalid listening window identity')
    elif surface == 'album_menu_refresh':
        if set(value) != {'epoch', 'exclude'}:
            raise ValueError('Unclassified album refresh fields')
        _integer(value['epoch'])
        _strings(value['exclude'])
    else:
        if 'epoch' not in value or set(value) - {'epoch', 'include_genres', 'avoid_genres', 'held_by_vibe', 'suspended_by_vibe'}:
            raise ValueError('Unclassified Home genre demand fields')
        _integer(value['epoch'])
        for field in ('include_genres', 'avoid_genres'):
            if field in value:
                _strings(value[field])
        for field in ('held_by_vibe', 'suspended_by_vibe'):
            if field in value and type(value[field]) is not bool:
                raise ValueError('Genre hold state requires a boolean')
    return value


def write(conn, surface, value, at):
    album_note_requests._transaction(conn)
    parse(surface, value, at)
    if surface.startswith('choice_window:'):
        format = surface.split(':', 1)[1]
        conn.execute('DELETE FROM listening_choice_windows WHERE format=?', (format,))
        if value:
            conn.execute('INSERT INTO listening_choice_windows VALUES (?,?,?,?)',
                         (format, value['opportunity_id'], value['window'], at))
    elif surface == 'album_menu_refresh':
        conn.execute('DELETE FROM album_menu_refresh_exclusions')
        conn.execute('DELETE FROM album_menu_refresh_state')
        conn.execute('INSERT INTO album_menu_refresh_state VALUES (1,?,?)', (value.get('epoch'), at))
        for position, browse in enumerate(value.get('exclude', [])):
            reference, reason = album_note_requests._reference(conn, browse)
            conn.execute('INSERT INTO album_menu_refresh_exclusions VALUES (1,?,?,\'youtube\',?,?)',
                         (position, browse, reference, reason))
    else:
        conn.execute('DELETE FROM home_genre_request_items')
        conn.execute('DELETE FROM home_genre_requests')
        conn.execute('INSERT INTO home_genre_requests VALUES (1,?,?,?,?,?,?)',
                     (value.get('epoch'), int('include_genres' in value), int('avoid_genres' in value),
                      int(value['held_by_vibe']) if 'held_by_vibe' in value else None,
                      int(value['suspended_by_vibe']) if 'suspended_by_vibe' in value else None, at))
        for field, kind in (('include_genres', 'include'), ('avoid_genres', 'avoid')):
            for position, raw in enumerate(value.get(field, [])):
                family = conn.execute('SELECT key FROM genre_family WHERE key=?', (raw,)).fetchone()
                genre, reason = (None, None) if family else genre_identity.resolve(conn, raw.removeprefix('other:'))
                conn.execute('INSERT INTO home_genre_request_items VALUES (1,?,?,?,?,?,?)',
                             (kind, position, raw, family[0] if family else None, genre,
                              reason if reason else None))


def read(conn, surface):
    if surface.startswith('choice_window:'):
        row = conn.execute('SELECT opportunity_key,window_text FROM listening_choice_windows WHERE format=?',
                            (surface.split(':', 1)[1],)).fetchone()
        return {'opportunity_id': row[0], 'window': row[1]} if row else None
    if surface == 'album_menu_refresh':
        row = conn.execute('SELECT epoch FROM album_menu_refresh_state WHERE id=1').fetchone()
        return ({'epoch': row[0], 'exclude': [item[0] for item in conn.execute(
            'SELECT browse_id FROM album_menu_refresh_exclusions ORDER BY position')]} if row and row[0] is not None
                else {} if row else None)
    if surface != 'home_genre_request':
        raise ValueError('Unknown recommendation demand owner')
    row = conn.execute('SELECT * FROM home_genre_requests WHERE id=1').fetchone()
    if row is None:
        return None
    result = {'epoch': row['epoch']} if row['epoch'] is not None else {}
    for field, flag, kind in (('include_genres', 'has_includes', 'include'), ('avoid_genres', 'has_avoids', 'avoid')):
        if row[flag]:
            result[field] = [item[0] for item in conn.execute(
                'SELECT raw_key FROM home_genre_request_items WHERE kind=? ORDER BY position', (kind,))]
    for field in ('held_by_vibe', 'suspended_by_vibe'):
        if row[field] is not None:
            result[field] = bool(row[field])
    return result
