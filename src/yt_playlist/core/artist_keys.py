"""Read canonical artist references without invoking catalog ingestion."""
from yt_playlist.util.matching import normalize


def canonical_id(conn, artist_id):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='artist_redirects'").fetchone():
        row = conn.execute('SELECT artist_id FROM artist_redirects WHERE retired_id=?',(artist_id,)).fetchone()
        if row:
            return row[0]
    return artist_id


def lookup(conn, value, *, exact=False):
    if isinstance(value,int) or str(value).startswith('@artist:'):
        try:
            artist_id = int(str(value).removeprefix('@artist:'))
        except ValueError:
            return None
        return canonical_id(conn,artist_id) if conn.execute('SELECT 1 FROM artists WHERE id=?',(artist_id,)).fetchone() else None
    rows = conn.execute('SELECT DISTINCT artist_id FROM artist_aliases WHERE alias=?',(value,)).fetchall()
    if not rows and not exact:
        rows = [(r['artist_id'],) for r in conn.execute('SELECT artist_id,alias FROM artist_aliases')
                if normalize(r['alias'])==normalize(value)]
    ids = {canonical_id(conn,r[0]) for r in rows}
    return next(iter(ids)) if len(ids)==1 else None
