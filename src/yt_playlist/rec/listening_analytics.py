"""Compatibility entry points for listening capture and prepared analytics."""
import json

from yt_playlist.library.listening_analytics_composition import preparation, calculate_from_reader
from yt_playlist.rec.listening_calculations import SCHEMA, FORMATS, FIELDS, number, document, partition


def capture_start(store, identity, video, playlist, status, at, *, album=False, inline=False):
    from yt_playlist.core.features import listening_charts_enabled
    if not listening_charts_enabled():
        return
    args = (identity, 'listen_start', video, None, None, playlist,
            json.dumps({'rating': status, 'album': album}), at)
    if inline:
        store.player_events.record_player_event(*args)
    else:
        capture = getattr(store, 'choice_capture', None)
        if capture is not None:
            capture.enqueue('listening analytics', store.player_events.record_player_event, *args)


def calculate(reader, now):
    return calculate_from_reader(reader, now)


def build(store, now):
    return preparation(store).build(now)
