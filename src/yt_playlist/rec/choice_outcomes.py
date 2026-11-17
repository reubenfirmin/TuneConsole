"""Compatibility entry points for verified playback-attribution commands."""
from yt_playlist.library.choice_composition import playback


from yt_playlist.core.playback_attribution import album_context


def bind(store, account_id, playlist, video, opportunity, option, now):
    return playback(store).bind(account_id, playlist, video, opportunity, option, now)


def observe(store, account_id, playlist, video, kind, position, duration, event_id, now):
    return playback(store).observe(account_id, playlist, video, kind, position, duration, event_id, now)


def continue_playback(store, account_id, playlist, video, now):
    return playback(store).continue_playback(account_id, playlist, video, now)
