"""Compatibility composition for Spotify's explicit import controller."""
from yt_playlist.controllers.spotify_library import (
    SpotifyLibrary, SpotifyLibraryFormatError, SPOTIFY_REPORT_SCHEMA,
    load_spotify_library, find_album_candidates, recording_match, album_candidates,
    _order_imported_tracks, _playlist_transfers, _fingerprint, _song_key,
    _album_title, _album_artist, _album_skip_reason, _album_fingerprint,
    _spotify_url,
)
from yt_playlist.controllers import spotify_library as acquisition
from yt_playlist.library import album_writes
from yt_playlist.library.action_claim import claim_action


def commands(store):
    return SpotifyLibrary(accounts=store.accounts, collection=store.collection,
        playlists=store.playlists, settings=store.settings,
        claim=lambda *args, **kw: claim_action(store, *args, **kw),
        save_album=lambda *args, **kw: album_writes.set_saved(store, *args, **kw))


def prepare_spotify_library(parsed, client, *, store=None, account_id=None):
    if store is None:
        return acquisition.prepare_spotify_library(parsed, client)
    return commands(store).prepare(parsed, client, account_id)


def import_spotify_library(store, raw, client, account_id, now, *, prepared=None):
    return commands(store).run(raw, client, account_id, now, prepared=prepared)


def save_selected_album(store, client, item, browse_id, *, account_id):
    return commands(store).resolve(client, item, browse_id, account_id=account_id)


def _save_album(store, client, candidate, account_id):
    return acquisition._save_album(commands(store), client, candidate, account_id)
