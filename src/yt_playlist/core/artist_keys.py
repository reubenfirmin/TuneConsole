"""Read canonical artist references without invoking catalog ingestion."""
from yt_playlist.util.matching import normalize


def canonical_id(conn, artist_id):
    row = conn.execute('SELECT COALESCE(redirect_artist_id,id) FROM artists WHERE id=?', (artist_id,)).fetchone()
    return row[0] if row else artist_id


def lookup(conn, value, *, exact=False):
    if isinstance(value,int) or str(value).startswith('@artist:'):
        try:
            artist_id = int(str(value).removeprefix('@artist:'))
        except ValueError:
            return None
        row = conn.execute('SELECT COALESCE(redirect_artist_id,id) FROM artists WHERE id=?', (artist_id,)).fetchone()
        return row[0] if row else None
    aliases = ('SELECT COALESCE(a.redirect_artist_id,a.id) AS artist_id,l.alias '
               'FROM artist_aliases l JOIN artists a ON a.id=l.artist_id')
    rows = conn.execute(aliases + ' WHERE l.alias=?', (value,)).fetchall()
    if not rows and not exact:
        normalized = normalize(value)
        rows = [row for row in conn.execute(aliases) if normalize(row['alias']) == normalized]
    ids = {row[0] for row in rows}
    return next(iter(ids)) if len(ids)==1 else None
