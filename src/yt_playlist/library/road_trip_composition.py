"""Compose bounded draft acquisition with existing repository/provider owners."""
from contextlib import contextmanager, nullcontext

from yt_playlist.controllers.road_trip_transitions import RoadTripFacts, RoadTripTransitions


def transitions(store):
    @contextmanager
    def inputs():
        owned = getattr(store, '_read_only', False) or store._lock.held_by_current_thread
        with (nullcontext(store) if owned else store.read_view()) as reader:
            yield RoadTripFacts(reader.rec_query, reader.rec_model, reader.genres)
    return RoadTripTransitions(inputs)


def acquisition(store):
    from yt_playlist.controllers.road_trip_acquisition import RoadTripAcquisition
    from yt_playlist.rec import road_trip_candidates as candidates
    return RoadTripAcquisition(transitions(store),
        fetch=lambda client, request: candidates.acquire_input(client, store, request),
        project=lambda rows: candidates.to_candidates(rows, store),
        durations=lambda client, rows, **kw: candidates._resolve_durations(store, client, rows, **kw),
        facts=lambda title, artist: candidates.track_facts(store, title, artist),
        context=candidates.artist_context, songs=candidates.artist_songs,
        feature_axes=store.genres.feature_axes)


def drafts(store, *, now, work, spawn):
    from yt_playlist.controllers.road_trip_drafts import RoadTripDrafts
    from yt_playlist.rec import measurement, road_trip_candidates as candidates
    return RoadTripDrafts(store.road_trip, transitions(store), acquisition(store), now=now,
        work=work, spawn=spawn, artist_genre=lambda name: candidates.artist_genre(store, name),
        served=store.rec_ledger.has_served_request, identity=store.tracks.identity_key_for_video,
        record=lambda *args, **kw: measurement.record_recommendations(store, *args, **kw))
