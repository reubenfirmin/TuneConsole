"""Compatibility entry points for the reusable synchronization controller."""
from yt_playlist.controllers.library_synchronization import (
    _artist_id, _album_id, _complete_rating_context, _is_auth_error, _first_playlist_order,
)


def sync_all(store, clients, now, **options):
    from yt_playlist.library.sync_composition import synchronization
    return synchronization(store).run(clients, now, **options)


def sync_identity(store, account_id, client, now, on_progress=None, label=None,
                  on_auth_expired=None, on_auth_ok=None, **options):
    from yt_playlist.library.sync_composition import synchronization
    return synchronization(store).identity(account_id, client, now, on_progress=on_progress,
        label=label, on_auth_expired=on_auth_expired, on_auth_ok=on_auth_ok, **options)


def refresh_playlist(store, account_id, client, provider_playlist_id, title, now, **options):
    from yt_playlist.library.sync_composition import synchronization
    return synchronization(store).refresh(account_id, client, provider_playlist_id, title, now, **options)


def _sync_saved_albums(store, clients, on_progress, **options):
    from yt_playlist.library.sync_composition import synchronization
    return synchronization(store).saved_albums(clients, on_progress, **options)


def _materialize_album_tracks(store, clients, saved, on_progress):
    from yt_playlist.library.sync_composition import synchronization
    return synchronization(store).album_tracks(clients, saved, on_progress)
