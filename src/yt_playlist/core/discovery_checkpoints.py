"""Discovery fetch history is retained independently of disposable candidates."""
from . import albums, discovery_sources, discovery_provenance


def fetched(conn, tokens):
    wanted = {discovery_sources.source_identity(token): token for token in tokens}
    if not wanted:
        return set()
    markers = conn.execute('''SELECT f.value->>0,f.value->>1 FROM discovery_origins o,json_each(o.fetch_sources) f
        WHERE o.fetch_sources<>'[]' ''')
    return {wanted[kind, key] for kind, key in markers if (kind, key) in wanted}


def mark(conn, kind, identifier, *, album=None):
    token = identifier if kind == 'album' else f'{kind}:{identifier}'
    fields = discovery_provenance.origin_fields(conn, token)
    if album is not None:
        fields.update(kind='album', album_id=album, seed_video_id=None, genre_key=None, artist_id=None,
                      legacy_source_key=None, resolution_status='resolved', resolution_reason=None)
    origin = discovery_provenance.store_origin(conn, fields)
    if kind == 'album' and album is None:
        return
    conn.execute('''UPDATE discovery_origins SET fetch_sources=json_insert(fetch_sources,'$[#]',json_array(?,?))
        WHERE id=? AND NOT EXISTS(SELECT 1 FROM json_each(fetch_sources) f WHERE f.value->>0=? AND f.value->>1=?)''',
        (kind, identifier, origin, kind, identifier))


def remember(conn, tokens):
    tokens = tuple(dict.fromkeys(tokens))
    if any(not isinstance(token, str) or not token for token in tokens):
        raise ValueError('Discovery fetch history requires its exact source token')
    retained = fetched(conn, tokens)
    for token in tokens:
        if token in retained:
            continue
        kind, identifier = discovery_sources.source_identity(token)
        if not identifier:
            raise ValueError('Discovery fetch history requires its exact source identifier')
        album = albums.lookup(conn, identifier) if kind == 'album' else None
        mark(conn, kind, identifier, album=album)
