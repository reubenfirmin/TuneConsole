"""Discovery fetch history is retained independently of disposable candidates."""
from . import albums, discovery_sources, discovery_provenance

PREFIX = 'discovery_tracks_fetched:'


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='discovery_fetch_history'").fetchone() is not None


def fetched(conn,token):
    if not available(conn):
        from .discovery_checkpoint_reads import OWNERS
        owner = OWNERS['fetch-history']
        if (revision := owner.selected(conn)) is not None:
            return owner.read(conn, revision, PREFIX+token) is not None
        row = conn.execute('SELECT value FROM settings WHERE key=?',(PREFIX+token,)).fetchone()
        return bool(row and row[0])
    kind,identifier = discovery_sources.source_identity(token)
    return conn.execute('SELECT 1 FROM discovery_fetch_history WHERE source_type=? AND source_id=?',
                        (kind,identifier)).fetchone() is not None


def remember(conn,token):
    if not isinstance(token,str) or not token:
        raise ValueError('Discovery fetch history requires its exact source token')
    if not available(conn):
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?,'1')",(PREFIX+token,))
        return
    kind,identifier = discovery_sources.source_identity(token)
    album = albums.lookup(conn,identifier) if kind=='album' else None
    if kind=='album' and album is None:
        if discovery_provenance.available(conn):
            # Unknown discovery input is retained by its provenance owner; a
            # fetch checkpoint cannot manufacture an album to satisfy its FK.
            discovery_provenance.origin(conn,token)
            return
        album = albums.observe(conn,identifier)
    conn.execute('INSERT INTO discovery_fetch_history(source_type,source_id,album_id) VALUES (?,?,?) '
                 'ON CONFLICT(source_type,source_id) DO NOTHING',(kind,identifier,album))
