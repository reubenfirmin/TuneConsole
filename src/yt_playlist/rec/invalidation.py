"""Recommendation invalidation policies. Callers own the transaction and serving lock."""


def queue_refresh(conn):
    """Coalesce a rebuild on current schemas; historical schemas have no worker queue."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='rec_change_queue'").fetchone():
        return False
    conn.execute("INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
                 'ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1')
    return True


def _invalidate_selection(conn):
    # Retain rollback evidence without restoring models from before an identity change.
    conn.execute('UPDATE rec_model_selection_runs SET identity_version=0 WHERE identity_version=2')


def catalog_changed(conn):
    """Track consumers filter superseded keys, so keep their completed generation while fitting."""
    if not queue_refresh(conn):
        for table in ('rec_vectors','rec_content_vectors','rec_discovered_content_vectors',
                      'rec_artist_vectors','rec_artist_content_vectors','rec_proposals'):
            conn.execute(f'DELETE FROM {table}')
        conn.execute("DELETE FROM settings WHERE key IN "
                     "('rec_content_model','rec_content_cov_bucket','rec_content_genre_revision')")
    _invalidate_selection(conn)


def artist_reconciled(conn):
    """Drop artist spaces eagerly: their vocabulary and cached proposals can contain retired IDs.

    Unlike track playback eligibility, artist-vector/proposal reads do not filter redirects.
    Keeping those rows would score a retired artist as a second candidate, or reinterpret its
    vector under the surviving identity. Preserve independent track spaces; queue a coherent
    rebuild and clear the artist fingerprint so even unchanged inputs repopulate emptied caches.
    """
    for table in ('rec_artist_vectors','rec_artist_content_vectors','rec_proposals'):
        conn.execute(f'DELETE FROM {table}')
    conn.execute("DELETE FROM settings WHERE key IN ('rec_artist_content_model','rec_artist_fit_inputs')")
    queue_refresh(conn)
    _invalidate_selection(conn)
