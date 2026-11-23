"""Read and retire the normalized Home display used during trial retirement."""
from .. import home_outputs
from ..policy_config import HOME_CONTEXTS


def encoded(conn, surface):
    row = conn.execute('SELECT id,content_digest,total_bytes,part_count FROM home_display_publications '
                       'WHERE surface=? AND is_current=1', (surface,)).fetchone()
    if row is None:
        return None
    identity, digest, size, count = row
    parts = conn.execute('SELECT chunk_key,filename,content_digest,byte_size FROM home_display_files '
                         'WHERE publication_id=? ORDER BY CAST(chunk_key AS INTEGER)', (identity,)).fetchall()
    if [part[0] for part in parts] != list(map(str, range(count))):
        raise ValueError('Incomplete Home display parts')
    return home_outputs.read_manifest(surface, digest, size, parts)


def clear(conn, surface):
    row = conn.execute('UPDATE home_display_publications SET is_current=0 '
                       'WHERE surface=? AND is_current=1 RETURNING id', (surface,)).fetchone()
    if row is None:
        return
    identity = row[0]
    conn.execute("UPDATE home_display_publications SET status='retiring' WHERE id=?", (identity,))
    for table in ('home_display_group_roots', 'home_display_recommendation_roots',
                  'home_display_policies', 'home_display_files'):
        conn.execute(f'DELETE FROM {table} WHERE publication_id=?', (identity,))
    for field in (*HOME_CONTEXTS, 'recommendations'):
        key = f'home-output:{identity}:{field}'
        release_group_context(conn, 'prepared', key)
        conn.execute("DELETE FROM recommendation_generation_refs WHERE kind='prepared' AND external_key=?", (key,))
    conn.execute('DELETE FROM home_display_publications WHERE id=?', (identity,))


def release_group_context(conn, kind, identity):
    for table in ('group_evidence_generations', 'group_evidence_versions', 'unresolved_group_evidence', 'group_evidence_owners'):
        conn.execute(f'DELETE FROM {table} WHERE kind=? AND external_key=?', (kind, identity))
