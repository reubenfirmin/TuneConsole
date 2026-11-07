"""Coalesced release-note demand, independently of disposable note output."""
from .recommendation_work import finite

MAX_REQUESTS = 24
FIELDS = frozenset({'browse_id', 'artist', 'title', 'year', 'thumbnail', 'requested_until'})


def parse(value, built_at=None):
    if built_at is not None:
        finite(built_at)
    if not isinstance(value, dict) or len(value) > MAX_REQUESTS:
        raise ValueError('Album note demand must be a bounded release map')
    result = []
    for browse, album in value.items():
        if not isinstance(album, dict) or album.keys() - FIELDS:
            raise ValueError('Unclassified album note demand fields')
        if album.get('browse_id') != browse:
            raise ValueError('Album note demand has conflicting release identities')
        for field in ('browse_id', 'artist', 'title'):
            if not isinstance(album.get(field), str) or not album[field]:
                raise ValueError('Album note demand requires release ID, artist and title')
        year = album.get('year')
        if year is not None and type(year) not in (str, int):
            raise ValueError('Album note year must retain text, integer or unknown')
        if type(year) is int and not -(2 ** 63) <= year < 2 ** 63:
            raise ValueError('Album note year exceeds SQLite integer storage')
        if album.get('thumbnail') is not None and not isinstance(album['thumbnail'], str):
            raise ValueError('Album note thumbnail must be text or unknown')
        if 'requested_until' in album:
            finite(album['requested_until'])
        result.append((browse, album['artist'], album['title'],
                       year if isinstance(year, str) else None,
                       year if type(year) is int else None,
                       album.get('thumbnail'), album.get('requested_until')))
    return result


def _transaction(conn):
    if not conn.in_transaction:
        raise RuntimeError('Album note demand requires an outer writer transaction')


def _reference(conn, browse):
    known = conn.execute("SELECT 1 FROM album_provider_ids WHERE provider='youtube' AND provider_id=?",
                         (browse,)).fetchone()
    return (browse, None) if known else (None, 'provider_album_not_admitted')


def _album(row):
    result = {'browse_id': row['browse_id'], 'artist': row['artist'], 'title': row['title'],
              'year': row['year_number'] if row['year_number'] is not None else row['year_text'],
              'thumbnail': row['thumbnail']}
    if row['requested_until'] is not None:
        result['requested_until'] = row['requested_until']
    return result


def read(conn):
    return {row['browse_id']: _album(row) for row in conn.execute('SELECT * FROM album_note_requests ORDER BY id')}


def next_request(conn):
    row = conn.execute('SELECT * FROM album_note_requests ORDER BY id LIMIT 1').fetchone()
    return (_album(row), (row['id'], row['work_revision'] if 'work_revision' in row.keys() else row['revision'])) if row else (None, None)


def matches(conn, browse, expected):
    row = conn.execute('SELECT * FROM album_note_requests WHERE browse_id=? AND id=?',
                        (browse, expected[0])).fetchone()
    return row is not None and (row['work_revision'] if 'work_revision' in row.keys() else row['revision']) == expected[1]


def complete(conn, browse, expected=None):
    _transaction(conn)
    if expected is None:
        conn.execute('DELETE FROM album_note_requests WHERE browse_id=?', (browse,))
    else:
        if matches(conn, browse, expected):
            conn.execute('DELETE FROM album_note_requests WHERE browse_id=? AND id=?', (browse, expected[0]))


def _insert(conn, fields, at, *, historical=False):
    browse, artist, title, year_text, year_number, thumbnail, until = fields
    reference, reason = _reference(conn, browse)
    conn.execute('INSERT INTO album_note_requests(browse_id,album_provider_id,unresolved_reason,artist,title,'
                 'year_text,year_number,thumbnail,requested_until,revision,requested_at,updated_at) '
                 'VALUES (?,?,?,?,?,?,?,?,?,1,?,?)',
                 (browse, reference, reason, artist, title, year_text, year_number, thumbnail, until,
                  None if historical else at, at))


def replace(conn, value, at, *, historical=False):
    _transaction(conn)
    records = parse(value, at)
    conn.execute('DELETE FROM album_note_requests')
    for fields in records:
        _insert(conn, fields, at, historical=historical)


def request(conn, albums, cache, now, *, current_only=False, visible=False):
    _transaction(conn)
    finite(now)
    albums = tuple(albums)
    current_ids = {album['browse_id'] for album in albums}
    if current_only:
        for row in conn.execute('SELECT browse_id,requested_until FROM album_note_requests').fetchall():
            if row['browse_id'] not in current_ids and (row['requested_until'] is None or row['requested_until'] <= now):
                complete(conn, row['browse_id'])
    for album in albums:
        browse = album['browse_id']
        cached = cache.get(browse, {})
        if (not album.get('artist') or not album.get('title')
                or cached.get('editorial_version') == 7 and cached.get('retry_at', 0) > now):
            continue
        previous = conn.execute('SELECT * FROM album_note_requests WHERE browse_id=?', (browse,)).fetchone()
        until = previous['requested_until'] if previous else None
        if visible:
            until = max(until if until is not None else now, now + 900)
        value = {key: album.get(key) for key in ('browse_id', 'artist', 'title', 'year', 'thumbnail')}
        if until is not None:
            value['requested_until'] = until
        fields = parse({browse: value}, now)[0]
        if previous:
            reference, reason = _reference(conn, browse)
            if _album(previous) != value or previous['album_provider_id'] != reference:
                before, after = _album(previous), dict(value)
                before.pop('requested_until', None)
                after.pop('requested_until', None)
                changed_work = before != after or previous['album_provider_id'] != reference
                fence = ',work_revision=work_revision+' + str(int(changed_work)) if 'work_revision' in previous.keys() else ''
                conn.execute('UPDATE album_note_requests SET album_provider_id=?,unresolved_reason=?,artist=?,title=?,'
                             'year_text=?,year_number=?,thumbnail=?,requested_until=?,revision=revision+1' + fence + ',updated_at=? '
                             'WHERE id=?', (reference, reason, *fields[1:], now, previous['id']))
        else:
            if conn.execute('SELECT count(*) FROM album_note_requests').fetchone()[0] == MAX_REQUESTS:
                conn.execute('DELETE FROM album_note_requests WHERE id=(SELECT min(id) FROM album_note_requests)')
            _insert(conn, fields, now)
