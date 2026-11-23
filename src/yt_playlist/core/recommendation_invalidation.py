"""Invalidate stored recommendation projections in the caller's transaction."""
from .content_preparation_state import REFRESH_REVISION
from .worker_settings import increment_sql

SERVING_SURFACES = ('home_prepared', 'home_tuned', 'album_prepared', 'home_discovery',
                    'home_revisit', 'into_recently', 'trend_rollups', 'cleanup')
REQUEST_REFRESH = increment_sql(REFRESH_REVISION)


def queue_refresh(conn):
    """Record that recommendation inputs changed in the caller's transaction."""
    conn.execute(REQUEST_REFRESH)


def _invalidate_selection(conn):
    # Retain rollback evidence without restoring models from before an identity change.
    conn.execute('UPDATE rec_model_selection_runs SET identity_version=0 WHERE identity_version=2')


def clear_display(conn, *, retain_serving=False):
    from yt_playlist.core import home_outputs, home_revisit_progress, listening_outputs
    surfaces = home_outputs.SURFACES - set(SERVING_SURFACES) if retain_serving else home_outputs.SURFACES
    home_outputs.clear(conn, surfaces)
    if not retain_serving:
        home_revisit_progress.clear(conn)
        listening_outputs.clear(conn)


def clear_findings(conn, *, retain_serving=False):
    from yt_playlist.core import choice_learning_state, recommendation_reports
    choice_learning_state.clear(conn)
    surfaces = recommendation_reports.SURFACES - {'listening_choices'} if retain_serving else recommendation_reports.SURFACES
    recommendation_reports.clear(conn, surfaces)
    if not retain_serving:
        from yt_playlist.core.tv_output_retention import clear_disposable
        clear_disposable(conn)
    conn.execute("UPDATE recommendation_generation_surfaces SET selected=0 WHERE selected=1 AND surface LIKE 'home_pool:%'")


def clear_model_display(conn):
    """Release the held menu after a daily model publication."""
    from yt_playlist.core import home_outputs
    conn.execute("DELETE FROM rec_proposals WHERE surface='home_choice'")
    home_outputs.clear(conn, ('home_choice',))


def catalog_changed(conn):
    """Track consumers filter superseded keys, so keep their completed generation while fitting."""
    queue_refresh(conn)
    _invalidate_selection(conn)


def artist_reconciled(conn):
    """Drop artist spaces eagerly: their vocabulary and cached proposals can contain retired IDs.

    Unlike track playback eligibility, artist-vector/proposal reads do not filter redirects.
    Keeping those rows would score a retired artist as a second candidate, or reinterpret its
    vector under the surviving identity. Preserve independent track spaces; queue a coherent
    rebuild and clear the artist fingerprint so even unchanged inputs repopulate emptied caches.
    """
    conn.execute('DELETE FROM artist_embedding_files')
    retained = (*SERVING_SURFACES, 'listening_choices', 'tv_pool')
    conn.execute(f'DELETE FROM rec_proposals WHERE surface NOT IN ({",".join("?" for _ in retained)})', retained)
    clear_display(conn, retain_serving=True)
    clear_findings(conn, retain_serving=True)
    from yt_playlist.core.model_state import ArtistModelUpdate, save_artist
    save_artist(conn, ArtistModelUpdate(encoder=None, fit_fingerprint=None), None)
    queue_refresh(conn)
    _invalidate_selection(conn)
