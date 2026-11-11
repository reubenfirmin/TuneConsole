"""Invalidate stored recommendation projections in the caller's transaction."""

SERVING_SURFACES = ('home_prepared', 'home_tuned', 'album_prepared', 'home_discovery',
                    'home_revisit', 'into_recently', 'trend_rollups', 'cleanup')


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


def clear_display(conn, *, retain_serving=False):
    from yt_playlist.core import home_outputs, listening_outputs
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='home_display_publications'").fetchone():
        surfaces = home_outputs.SURFACES - set(SERVING_SURFACES) if retain_serving else home_outputs.SURFACES
        home_outputs.clear(conn, surfaces)
    if not retain_serving and conn.execute("SELECT 1 FROM sqlite_schema WHERE name='home_revisit_progress'").fetchone():
        conn.execute('DELETE FROM home_revisit_progress')
    if not retain_serving and listening_outputs.available(conn):
        listening_outputs.clear(conn)


def clear_findings(conn, *, retain_serving=False):
    from yt_playlist.core import choice_learning_state, model_comparison_state, recommendation_reports
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='choice_learning_publications'").fetchone():
        choice_learning_state.clear(conn)
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_report_publications'").fetchone():
        surfaces = recommendation_reports.SURFACES - {'listening_choices'} if retain_serving else recommendation_reports.SURFACES
        recommendation_reports.clear(conn, surfaces)
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_comparison_plan'").fetchone():
        model_comparison_state.clear(conn)
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_pair_heads'").fetchone():
        from yt_playlist.core import model_projections
        model_projections.clear(conn)
    if not retain_serving and conn.execute("SELECT 1 FROM sqlite_schema WHERE name='tv_pool_state'").fetchone():
        from yt_playlist.core.tv_output_retention import clear_disposable
        clear_disposable(conn)
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='clicked_library_pools'").fetchone():
        conn.execute("DELETE FROM recommendation_heads WHERE surface LIKE 'home_pool:%'")


def clear_model_display(conn):
    """Release the held choice; completed menus serve until replacement succeeds.

    Serving readers recheck current catalog membership, feedback and preferences.
    Model freshness alone must not erase playable output during preparation.
    """
    from yt_playlist.core import home_outputs
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='home_display_publications'").fetchone():
        home_outputs.clear(conn, ('home_choice',))


def catalog_changed(conn):
    """Track consumers filter superseded keys, so keep their completed generation while fitting."""
    if not queue_refresh(conn):
        for table in ('rec_vectors','rec_content_vectors','rec_discovered_content_vectors',
                      'rec_artist_vectors','rec_artist_content_vectors','rec_proposals'):
            conn.execute(f'DELETE FROM {table}')
        clear_display(conn)
        clear_findings(conn)
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
    conn.execute('DELETE FROM artist_embeddings')
    from . import embedding_files
    if embedding_files.available(conn):
        conn.execute('DELETE FROM artist_embedding_files')
    retained = (*SERVING_SURFACES, 'listening_choices', 'tv_pool')
    conn.execute(f'DELETE FROM rec_proposals WHERE surface NOT IN ({",".join("?" for _ in retained)})', retained)
    clear_display(conn, retain_serving=True)
    clear_findings(conn, retain_serving=True)
    from yt_playlist.core import model_state
    if model_state.available(conn):
        model_state.put(conn,{'rec_artist_content_model':None,'rec_artist_fit_inputs':None})
    conn.execute("DELETE FROM settings WHERE key IN ('rec_artist_content_model','rec_artist_fit_inputs')")
    queue_refresh(conn)
    _invalidate_selection(conn)
