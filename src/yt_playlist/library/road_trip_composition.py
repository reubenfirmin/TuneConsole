"""Compose bounded draft acquisition with existing repository/provider owners."""
from contextlib import contextmanager, nullcontext

from yt_playlist.controllers.road_trip_transitions import RoadTripFacts, RoadTripTransitions
from yt_playlist.rec import embedding_vectors, road_trip_ranking, scoring
from yt_playlist.rec.road_trip_artists import selected_artist
from yt_playlist.rec.road_trip_axes import canonical_genre
from yt_playlist.util.matching import identity_key


def _driver_scores(reader, now, collaborative, content, snapshot, candidate_keys):
    spaces = (
        (collaborative, candidate_keys, lambda: scoring.playlist_taste(reader, now=now), False),
        (content, candidate_keys.difference(collaborative.index),
         lambda: scoring.content_taste(reader, snapshot=snapshot, now=now), True),
    )
    scores = {}
    for (keys, matrix, index), candidates, taste_for, content_only in spaces:
        eligible = candidates.intersection(index)
        if not eligible:
            continue
        taste = taste_for()
        components = scoring.personalized_track_components(reader, taste, matrix, index, now,
            content_only=content_only, content_listening=True, content_snapshot=snapshot)
        base = components['combined']
        if taste or base.any():
            weighted = scoring._apply_axis_weights(reader, dict(zip(keys, map(float, base), strict=True)), now)
            scores.update((key, weighted[key]) for key in eligible)
    return scores


def _rank(reader, own, passengers, artists, genres, now, *, artist_refs=None):
    artist_refs = artist_refs or {}
    keys = {row["video_id"]: row.get("key") for row in own}
    candidate_keys = {key for key in keys.values() if key}
    observed = reader.tracks.identity_keys_for_videos([row["video_id"] for row in passengers])
    for row in passengers:
        video = row["video_id"]
        if video not in keys:
            key = observed.get(video)
            artist = selected_artist(row, artists, artist_refs)
            # A title/name match cannot prove that a provider upload belongs to
            # the selected recording when artists share a display name.
            if key is None and not (artist and artist.startswith('@artist:')):
                key = identity_key(row["title"], row["artist"])
            keys[video] = key
    empty = embedding_vectors.VectorSpace([], None, {})
    collaborative = embedding_vectors.load_vectors(reader) if candidate_keys else empty
    needs_content = artists or genres or candidate_keys.difference(collaborative.index)
    snapshot = reader.rec_model.content_generation() if needs_content else None
    content = (embedding_vectors.content_space(reader, include_new=True, snapshot=snapshot)
               if snapshot is not None else empty)
    driver_scores = _driver_scores(reader, now, collaborative, content, snapshot, candidate_keys)
    return road_trip_ranking.rank_tracks(own, passengers, artists, map(canonical_genre, genres),
                                         keys, content, driver_scores, artist_refs=artist_refs)


def transitions(store):
    @contextmanager
    def inputs():
        owned = getattr(store, '_read_only', False) or store._lock.held_by_current_thread
        with (nullcontext(store) if owned else store.read_view(tracked=True, pinned=True)) as reader:
            def excluded_videos(rows, now):
                videos = {row['video_id']: row for row in rows}
                known = reader.tracks.identity_keys_for_videos(list(videos))
                keys = {video: known.get(video) or identity_key(row['title'], row['artist'])
                        for video, row in videos.items()}
                blocked = reader.rec_model.disliked_identity_keys(keys=keys.values(), now=now)
                return frozenset(video for video, key in keys.items() if key in blocked)

            yield RoadTripFacts(reader.rec_query, reader.rec_model, reader.genres,
                                rank=lambda *args, **kwargs: _rank(reader, *args, **kwargs),
                                excluded_videos=excluded_videos)
    return RoadTripTransitions(inputs)


def acquisition(store):
    from yt_playlist.controllers.road_trip_acquisition import RoadTripAcquisition
    from yt_playlist.rec import road_trip_candidates as candidates
    return RoadTripAcquisition(transitions(store),
        fetch=lambda client, request: candidates.acquire_input(client, store, request),
        project=lambda rows: candidates.to_candidates(rows, store),
        durations=lambda client, rows, **kw: candidates._resolve_durations(store, client, rows, **kw),
        facts=lambda title, artist: candidates.track_facts(store, title, artist),
        context=lambda client, name, **kwargs: candidates.artist_context(client, name, store, **kwargs),
        songs=candidates.artist_songs,
        feature_axes=store.genres.feature_axes)


def drafts(store, *, now, work, spawn):
    from yt_playlist.controllers.road_trip_drafts import RoadTripDrafts
    from yt_playlist.rec import measurement, road_trip_candidates as candidates
    return RoadTripDrafts(store.road_trip, transitions(store), acquisition(store), now=now,
        work=work, spawn=spawn, artist_genre=lambda name: candidates.artist_genre(store, name),
        served=store.rec_ledger.has_served_request, identity=store.tracks.identity_key_for_video,
        record=lambda *args, **kw: measurement.record_recommendations(store, *args, **kw))
