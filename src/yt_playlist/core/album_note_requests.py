"""Coalesced editorial demand keyed by the exact provider release."""
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


def admit(conn, browse, fields=None):
    from . import albums

    album_id = albums.lookup(conn, browse)
    metadata = (None, None, None, None)
    if fields is not None:
        _, artist, title, year_text, year_number, thumbnail, _ = fields
        year = str(year_number) if year_number is not None else year_text
        metadata = (artist, title, year or None, thumbnail or None)
    # Editorial inputs can fill release metadata ahead of the catalog fetch.
    # Album credits are supplied by catalog observations.
    if album_id is None:
        album_id = conn.execute(
            'INSERT INTO albums(artist,title,year,thumbnail,metadata_rank) VALUES (?,?,?,?,-1)',
            metadata).lastrowid
        conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id) VALUES ('youtube',?,?)",
                     (browse, album_id))
    elif fields is not None:
        columns = ('artist', 'title', 'year', 'thumbnail')
        assignments = ','.join(f'{column}=CASE WHEN metadata_rank=-1 THEN COALESCE(:{column},{column}) '
                               f'ELSE COALESCE({column},:{column}) END' for column in columns)
        conn.execute(f'UPDATE albums SET {assignments} WHERE id=:id',
                     {'id': album_id, **dict(zip(columns, metadata, strict=True))})


def _album(row):
    year = row['year']
    if row['note_numeric_year'] and year is not None:
        try:
            year = int(year)
        except ValueError:
            pass
    value = dict(browse_id=row['provider_id'], artist=row['artist'], title=row['title'],
                 year=year, thumbnail=row['thumbnail'])
    if row['note_requested_until'] is not None:
        value['requested_until'] = row['note_requested_until']
    return value


def _rows(conn, *, first=False):
    return conn.execute("SELECT p.provider_id,p.note_requested_until,p.note_numeric_year,"
        "p.note_request_order,p.note_work_revision,a.artist,a.title,a.year,a.thumbnail "
        "FROM album_provider_ids p JOIN albums a ON a.id=p.album_id "
        "WHERE p.note_pending=1 ORDER BY p.note_request_order" + (" LIMIT 1" if first else ""))


def read(conn):
    return {row['provider_id']: _album(row) for row in _rows(conn)}


def next_request(conn):
    row = _rows(conn, first=True).fetchone()
    return (_album(row), (row['note_request_order'], row['note_work_revision'])) if row else (None, None)


def matches(conn, browse, expected):
    return conn.execute("SELECT 1 FROM album_provider_ids WHERE provider='youtube' AND provider_id=? "
        "AND note_pending=1 AND note_request_order=? AND note_work_revision=?",
        (browse, *expected)).fetchone() is not None


def complete(conn, browse, expected=None):
    _transaction(conn)
    if expected is None or matches(conn, browse, expected):
        conn.execute("UPDATE album_provider_ids SET note_pending=0,note_requested_until=NULL "
                     "WHERE provider='youtube' AND provider_id=? AND note_pending=1", (browse,))


def store(conn, fields):
    _transaction(conn)
    browse, _, _, _, year_number, _, until = fields
    admit(conn, browse, fields)
    numeric = int(year_number is not None)
    conn.execute("UPDATE album_provider_ids SET note_pending=1,note_requested_until=?,note_numeric_year=?, "
                 "note_request_order=CASE WHEN note_pending=0 THEN "
                 "(SELECT COALESCE(MAX(note_request_order),0)+1 FROM album_provider_ids) ELSE note_request_order END,"
                 "note_work_revision=note_work_revision+(note_pending=0 OR note_numeric_year<>?) "
                 "WHERE provider='youtube' AND provider_id=? "
                 "AND (note_pending=0 OR note_requested_until IS NOT ? OR note_numeric_year<>?)",
                 (until, numeric, numeric, browse, until, numeric))


def replace(conn, value, at):
    _transaction(conn)
    records = parse(value, at)
    conn.execute("UPDATE album_provider_ids SET note_pending=0,note_requested_until=NULL WHERE note_pending=1")
    for fields in records:
        store(conn, fields)
