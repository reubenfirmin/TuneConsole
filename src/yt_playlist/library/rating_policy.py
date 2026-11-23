"""Library rating policy shared by catalog and recommendation projections.

The active master account's recorded rating, including confirmed writes not yet
synced, takes precedence over cached Liked Music membership. Queries alias tracks
as ``t`` when executing this domain-owned projection.
"""
from contextlib import contextmanager

from yt_playlist.core.db_revisions import keyed_reads


def liked_exists(playlist_ids_sql):
    return ("COALESCE((SELECT r.status='LIKE' FROM item_ratings r "
            "JOIN accounts i ON i.id=r.account_id WHERE i.active=1 AND i.is_master=1 "
            "AND r.video_id=t.video_id), EXISTS(SELECT 1 FROM playlist_tracks lpt "
            "JOIN catalog_tracks lt ON lt.id=lpt.track_id "
            f"WHERE lpt.playlist_id IN ({playlist_ids_sql}) AND lt.video_id=t.video_id))")


LIKED_EXISTS = liked_exists("SELECT id FROM active_playlists WHERE provider_playlist_id='LM'")


@contextmanager
def liked_music_selection(store):
    """Bind the Liked Music projection and guard only its playlist membership."""
    # The provider key also guards scopes for Liked Music lists that have yet to sync.
    with keyed_reads(store, 'playlist_scopes', ('LM',)):
        scopes = tuple(row[0] for row in store.conn.execute(
            "SELECT id FROM playlist_scopes WHERE provider_playlist_id='LM' AND kind='account'"))
    with keyed_reads(store, 'playlists', scopes):
        slots = ','.join('?' for _ in scopes)
        likes = tuple(row[0] for row in store.conn.execute(
            f'SELECT id FROM playlists WHERE is_current=1 AND playlist_scope_id IN ({slots})', scopes))
    selection = 'VALUES ' + ','.join('(?)' for _ in likes) if likes else 'SELECT NULL WHERE 0'
    with keyed_reads(store, 'playlist_items', likes):
        yield liked_exists(selection), likes
