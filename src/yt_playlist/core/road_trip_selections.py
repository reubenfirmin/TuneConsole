"""Ordered recipe selections with the captured label and its identity."""
from .artist_policy import token


def project(conn, selections):
    artist_rows = conn.execute("""
        SELECT coalesce(a.name,j.value->>'$.label'),
               CASE WHEN a.provisional=0 THEN a.id END,
               (SELECT min(p.provider_id) FROM artist_provider_ids p
                WHERE p.artist_id=a.id AND p.provider='youtube')
        FROM json_each(?,'$.artists') j
        LEFT JOIN artists stored ON stored.id=j.value->>'$.artist_id'
        LEFT JOIN artists a ON a.id=coalesce(stored.redirect_artist_id,stored.id)
        ORDER BY j.key
    """, (selections,)).fetchall()
    result = {'artists': [row[0] for row in artist_rows], 'genres': [], 'blacklist_genres': []}
    if any(identity is not None for _, identity, _ in artist_rows):
        result['artist_refs'] = {token(identity) if identity is not None else name:
                                 {'name': name, 'browse_id': browse} for name, identity, browse in artist_rows}
    for role, label in conn.execute("""
        SELECT j.value->>'$.role',coalesce(c.name,j.value->>'$.label')
        FROM json_each(?,'$.genres') j
        LEFT JOIN genre_catalog stored ON stored.key=j.value->>'$.genre_key'
        LEFT JOIN genre_catalog c ON c.key=coalesce(stored.redirect_key,stored.key)
        ORDER BY j.key
    """, (selections,)):
        result['genres' if role == 'include' else 'blacklist_genres'].append(label)
    return result
