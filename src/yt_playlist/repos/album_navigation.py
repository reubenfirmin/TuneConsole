"""Exact release identities for track labels whose provider metadata omitted an ID."""


def track_album_browse_ids(conn, memberships, entry_ids):
    """Resolve only confirmed entry membership matching the displayed album label.

    A recording can appear on several releases. Neither a shared recording nor
    a matching album name elsewhere in the catalog identifies this entry's album.
    Multiple matching releases remain ambiguous even when their labels agree.
    """
    result = {}
    for offset in range(0, len(entry_ids), 500):
        batch = entry_ids[offset:offset + 500]
        rows = conn.execute(
            'SELECT t.id,MIN(p.provider_id) browse FROM catalog_tracks t '
            f'JOIN {memberships} m ON m.entry_id=t.id '
            'JOIN albums a ON a.id=m.album_id AND a.title=t.album '
            "JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            'WHERE t.id IN (' + ','.join('?' for _ in batch) + ") AND p.provider_id<>'' "
            'GROUP BY t.id HAVING COUNT(DISTINCT a.id)=1', batch).fetchall()
        result.update((row['id'], row['browse']) for row in rows)
    return result
