"""Bind shared generated commands for background consumers without HTTP resources."""
from yt_playlist.controllers.generated_playlists import GeneratedPlaylists
from yt_playlist.core.accounts import AccountAccess
from yt_playlist.library import generated_acquisition as playlist_generated
from yt_playlist.rec import choice_evidence, measurement


def commands(store, now):
    return GeneratedPlaylists(AccountAccess(store.accounts, store.playlists, {}), None, store.modes,
        create=lambda *args, **kwargs: playlist_generated.create_generated_playlist(store, *args, **kwargs),
        now=lambda: now, measure=lambda *args, **kwargs: measurement.record_action(store, *args, **kwargs),
        respond=lambda *args, **kwargs: choice_evidence.safe_respond(store, *args, **kwargs),
        enrich=lambda pid: None, navigate=lambda *args: None, refresh=lambda: None,
        query=lambda account, remote: playlist_generated.saved_playlist_query(store, None, lambda: {}, account, remote),
        recipes=store.playlist_recipes)
