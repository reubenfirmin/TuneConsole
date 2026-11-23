"""Persisted discoveries and library evidence exercise the real cold scoring pipeline."""
import json

import numpy as np
import pytest

from tests.retained_rating_support import apply_model_ratings
from yt_playlist.rec import (artist_model, candidate_refresh, discover, embedding_vectors as embed, mode_surfaces,
                             rec_params, scoring, surfaces, transient)
from yt_playlist.util.matching import identity_key


@pytest.fixture
def cold_library(store, monkeypatch):
    # The separate genre overlay can reorder these same candidates, masking a broken vector
    # tilt. Hold that overlay neutral; projection, taste and both tilt producers stay real.
    monkeypatch.setattr(surfaces, "discovery_facet_weight", lambda *args, **kwargs: 1.0)
    iid = store.accounts.upsert_account("main", None, True)
    tids, rows, keys = [], [], []
    for genre, direction in (("Techno", [1, 0]), ("Folk", [0, 1])):
        for i in range(10):
            title, artist = f"{genre} {i}", f"{genre} artist {i}"
            tid = store.tracks.upsert_track(f"{genre}{i}", title, artist, None, 200)
            store.tracks.set_track_genre(tid, genre)
            store.tracks.set_track_year(tid, "2000")
            key = identity_key(title, artist)
            keys.append(key)
            tids.append(tid)
            rows.append((key, np.array(direction, dtype=np.float32).tobytes()))
    pid = store.playlists.upsert_playlist(iid, "PL_owned", "Owned", 20, "h", 0)
    store.playlists.set_playlist_tracks(pid, tids)
    store.rec_model.replace_rec_vectors(rows)
    store.rec_model.replace_rec_content_vectors(rows)
    cold = {}
    # Put the less favored item first, so a strict ranking change proves scoring occurred.
    for genre, direction in (("Folk", [0, 1]), ("Techno", [1, 0])):
        title, artist = f"Cold {genre}", f"New {genre} artist"
        key = identity_key(title, artist)
        cold[genre] = key
        store.discovery.upsert_discovered_track(key, f"cold{genre}", title, artist, "", None,
                                      genre, "2000", "MPRE_new", 10)
    store.rec_model.replace_rec_discovered_content_vectors([
        (cold[genre], np.array(direction, dtype=np.float32).tobytes())
        for genre, direction in (("Folk", [0, 1]), ("Techno", [1, 0]))])
    rec_params.set_param(store, "audio_transient_w", 0)
    rec_params.set_param(store, "session_alpha", 0)
    rec_params.set_param(store, "facet_gain", 0)
    assert discover.ContentProjection.fit(store) is not None
    assert set(cold.values()).isdisjoint(embed.load_vectors(store)[2])
    return iid, tids, keys, cold


def _rank(store):
    return [item.key for item in surfaces.cold_candidates(store, now=1000)]


def test_discovered_dislike_does_not_trigger_a_projection_without_taste(store, cold_library, monkeypatch):
    _, _, _, cold = cold_library
    monkeypatch.setattr(surfaces, 'playlist_taste', lambda *args, **kwargs: None)

    def forbidden(*args, **kwargs):
        pytest.fail('Exact exclusion must not fit a projection')

    monkeypatch.setattr(discover.ContentProjection, 'fit', forbidden)
    before = _rank(store)
    disliked = cold['Techno']
    assert disliked in before
    apply_model_ratings(store, {disliked: 'DISLIKE'}, 1000)
    assert _rank(store) == [key for key in before if key != disliked]
    apply_model_ratings(store, {disliked: 'INDIFFERENT'}, 1000)
    assert _rank(store) == before


@pytest.mark.parametrize('content_only', [False, True])
def test_owned_dislike_excludes_one_recording_without_changing_scores(store, cold_library, content_only):
    _, _, library_keys, cold = cold_library
    if content_only:
        store.rec_model.replace_rec_vectors([])
        snapshot = store.rec_model.content_generation()
        model = json.dumps({'cat': {'techno': 0, 'folk': 1}, 'ncat': 2, 'cont': []})
        assert store.rec_model.publish_content_generation(snapshot['library'], snapshot['discovered'],
            model, 'test-genre-revision', 'test-content-inputs', snapshot['generation'])
    now = 1000
    if not content_only:
        track_keys, matrix, index = embed.load_vectors(store)
        taste = scoring.playlist_taste(store, now=now)
        _, before = scoring.score_with_traces(store, taste, track_keys, matrix, index, now)
    else:
        assert discover.ContentProjection.fit(store) is None
    baseline = mode_surfaces.prepare_bundles(store, now)
    assert baseline
    assert not candidate_refresh.due(store, now)
    base_catalog = {row['key']: row['score'] for row in baseline['all']['explore']}
    base_fresh = {row['key']: row['score'] for row in baseline['all']['fresh']}
    assert base_catalog[library_keys[1]] == pytest.approx(base_catalog[library_keys[11]])
    assert base_fresh[cold['Techno']] == pytest.approx(base_fresh[cold['Folk']])

    disliked = library_keys[0]
    apply_model_ratings(store, {disliked: 'DISLIKE'}, now)
    assert candidate_refresh.due(store, now)
    adjusted = mode_surfaces.prepare_bundles(store, now)
    catalog = {row['key']: row['score'] for row in adjusted['all']['explore']}
    fresh = {row['key']: row['score'] for row in adjusted['all']['fresh']}
    assert disliked not in catalog
    assert catalog == {key: value for key, value in base_catalog.items() if key != disliked}
    assert fresh == base_fresh
    if not content_only:
        _, traces = scoring.score_with_traces(store, taste, track_keys, matrix, index, now)
        assert traces == before

    apply_model_ratings(store, {disliked: 'INDIFFERENT'}, now)
    assert candidate_refresh.due(store, now)
    cleared = mode_surfaces.prepare_bundles(store, now)
    assert {row['key']: row['score'] for row in cleared['all']['explore']} == base_catalog
    assert {row['key']: row['score'] for row in cleared['all']['fresh']} == base_fresh


def test_artist_ensemble_projects_a_lastfm_neighbor_without_audio_or_raw_artist_vectors(store, cold_library):
    account, tracks, _, _ = cold_library
    playlist = store.playlists.upsert_playlist(account, 'PL_favorite', 'Favorite sound', 10, 'h', 0)
    store.playlists.set_playlist_tracks(playlist, tracks[:10])
    warm_artist = store.artists.resolve('Techno artist 0')
    identity = store.artists.ensure_artist('Cold artist', provider='youtube', provider_id='UCcold0000')
    cold = store.discovery.upsert_discovered_track(
        'heard later|cold artist', 'cold-provider-only', 'Heard later', 'Cold artist', '', None,
        None, None, None, 10, credits=[{'name': 'Cold artist', 'id': 'UCcold0000'}])
    assert store.artists.track_keys(keys=[cold])[cold] == {store.artists.key(identity)}
    assert not store.rec_model.get_rec_artist_vectors()
    assert cold not in _rank(store)

    store.artist_similarity.cache_similar(warm_artist, [('Cold artist', .9)], 10)
    projection = discover.ContentProjection.fit(store)
    assert np.any(projection.predict(None, artist=store.artists.key(identity)))
    assert cold in _rank(store)

    same_name = store.artists.ensure_artist('Cold artist', provider='youtube', provider_id='UCcold1111')
    assert not np.any(projection.predict(None, artist=store.artists.key(same_name)))
    assert cold not in _rank(store)


def test_cold_projection_uses_shared_ranked_artist_neighbors(store, cold_library, monkeypatch):
    account, tracks, _, _ = cold_library
    playlist = store.playlists.upsert_playlist(account, 'PL_favorite', 'Favorite sound', 10, 'h', 0)
    store.playlists.set_playlist_tracks(playlist, tracks[:10])
    seed = store.artists.key('Techno artist 0')
    identity = store.artists.ensure_artist('Cold artist', provider='youtube', provider_id='UCcold0000')
    neighbor_key = store.artists.key(identity)
    cold = store.discovery.upsert_discovered_track(
        'heard later|cold artist', 'cold-mixed', 'Heard later', 'Cold artist', '', None,
        None, None, None, 10, credits=[{'name': 'Cold artist', 'id': 'UCcold0000'}])
    row = np.array([1., 0.], dtype=np.float32).tobytes()
    store.rec_model.replace_rec_artist_vectors([(seed, row), (neighbor_key, row)])
    store.artist_similarity.cache_similar(seed, [('Cold artist', .9)], 10)

    rank_neighbors = artist_model.ranked_artist_neighbors
    expected, = rank_neighbors(store, [seed])
    assert expected.key == neighbor_key
    assert expected.sources.keys() == {'co-occurrence', 'lastfm'}
    used = []

    def track_ranking(reader, seeds, **options):
        ranked = rank_neighbors(reader, seeds, **options)
        if seeds == [seed]:
            used.extend(ranked)
        return ranked

    monkeypatch.setattr(artist_model, 'ranked_artist_neighbors', track_ranking)
    projection = discover.ContentProjection.fit(store)
    assert used == [expected]
    assert projection.artist_features[seed] == pytest.approx([1.])
    assert projection.artist_features[neighbor_key] == pytest.approx([expected.score])
    predicted = projection.predict(None, artist=neighbor_key)
    assert np.any(predicted)
    np.testing.assert_allclose(predicted, projection.W[-1] * expected.score)
    assert cold in _rank(store)


@pytest.mark.parametrize('compatible', [True, False])
def test_discovered_dislike_only_changes_recording_eligibility(store, cold_library, compatible, monkeypatch):
    from yt_playlist.library.road_trip_composition import transitions

    _, _, library_keys, cold = cold_library
    near, other = library_keys[1], library_keys[11]
    disliked = cold['Techno'] if compatible else store.discovery.upsert_discovered_track(
        'missing|unknown', 'missing', 'Missing', 'Unknown', '', None, None, None, None, 10)
    now = 1000
    trip = transitions(store)
    state = {'inputs': {'artists': ['Techno artist 1']}}
    projection = discover.ContentProjection
    fit = projection._fit_library
    fits = []

    def count_fit(cls, *args):
        fits.append(True)
        return fit(*args)

    monkeypatch.setattr(projection, '_fit_library', classmethod(count_fit))

    def scores():
        prepared = mode_surfaces.prepare_bundles(store, now)
        catalog = {row['key']: row['score'] for row in prepared['all']['explore']}
        tracks = trip.capture(state, now).ranking
        with store.read_view(tracked=True, pinned=True) as reader:
            keys, matrix, index = embed.load_vectors(reader)
            assert disliked not in index
            taste = scoring.playlist_taste(reader, now=now)
            _, traces = scoring.score_with_traces(reader, taste, keys, matrix, index, now)
        return catalog, tracks, traces, {row['key']: row['score'] for row in prepared['all']['fresh']}

    before_catalog, before_trip, before_traces, before_fresh = scores()
    assert before_catalog[near] == before_catalog[other]
    assert before_trip.passenger_fit['Techno1'] == pytest.approx(1.)
    apply_model_ratings(store, {disliked: 'DISLIKE'}, now)
    assert candidate_refresh.due(store, now)
    catalog, ranked, traces, fresh = scores()
    assert (disliked in before_fresh) is compatible
    assert fresh == {key: value for key, value in before_fresh.items() if key != disliked}
    assert catalog == before_catalog
    assert traces == before_traces
    assert ranked == before_trip

    apply_model_ratings(store, {disliked: 'INDIFFERENT'}, now)
    assert candidate_refresh.due(store, now)
    assert scores() == (before_catalog, before_trip, before_traces, before_fresh)
    assert len(fits) == 1

    # New vectors can occupy the same dimensions with different coordinate meanings.
    keys, matrix, _ = embed.load_vectors(store)
    store.rec_model.replace_rec_vectors([(key, row[::-1].tobytes()) for key, row in zip(keys, matrix)])
    scores()
    assert len(fits) == 2

    # Content changes also invalidate the projection with that target matrix held fixed.
    track_id = cold_library[1][1]
    store.tracks.set_track_genre(track_id, 'Folk')
    scores()
    assert len(fits) == 3


def _content_only_play(store, iid, genre, direction):
    # The played track has a content vector but no collaborative vector. This isolates acoustic
    # ranking without disabling either real producer, now that the library collaborative tilt works.
    title, artist = "Audio evidence", "Library artist"
    key = identity_key(title, artist)
    tid = store.tracks.upsert_track("audioonly", title, artist, None, 200)
    store.tracks.set_track_genre(tid, genre)
    store.tracks.set_track_year(tid, "2000")
    keys, vectors, _ = embed.load_content_vectors(store)
    store.rec_model.replace_rec_content_vectors(
        [(k, vectors[i].tobytes()) for i, k in enumerate(keys)] +
        [(key, np.array(direction, dtype=np.float32).tobytes())])
    assert key not in embed.load_vectors(store)[2]
    store.history.record_play_event(iid, key, "audioonly", 999, playlist_ytm_id="PL_owned")


def test_real_projection_ranks_genre_only_discoveries_and_preserves_warm_scores(store, cold_library):
    iid, tids, keys, cold = cold_library
    favorite = store.playlists.upsert_playlist(iid, "PL_favorite", "Favorite sound", 10, "h", 0)
    store.playlists.set_playlist_tracks(favorite, tids[:10])
    vector_keys, vectors, idx = embed.load_vectors(store)
    taste = scoring.playlist_taste(store, now=1000)
    before = surfaces._score_candidates(store, taste, vector_keys, vectors, idx, now=1000)
    traced, _ = scoring.score_with_traces(store, taste, vector_keys, vectors, idx, now=1000)
    assert traced == before
    assert before[keys[0]] > before[keys[10]]
    assert _rank(store) == [cold["Techno"], cold["Folk"]]
    after = surfaces._score_candidates(store, scoring.playlist_taste(store, now=1000),
                                      vector_keys, vectors, idx, now=1000)
    assert after == before
    assert all(not row["audio"] for row in store.discovery.get_discovered_tracks())


def test_recent_audio_reranks_real_cold_candidates_and_zero_knob_disables_it(store, cold_library):
    iid, _, _, cold = cold_library
    assert _rank(store) == [cold["Folk"], cold["Techno"]]
    _content_only_play(store, iid, "Techno", [1, 0])
    assert transient.audio_centroid_tilt(store, 1000) is not None
    rec_params.set_param(store, "audio_transient_w", 1)
    assert _rank(store) == [cold["Techno"], cold["Folk"]]
    rec_params.set_param(store, "audio_transient_w", 0)
    assert _rank(store) == [cold["Folk"], cold["Techno"]]


def test_missing_discovered_audio_vectors_keeps_genre_ranking(store, cold_library):
    iid, tids, _, cold = cold_library
    favorite = store.playlists.upsert_playlist(iid, "PL_favorite", "Favorite sound", 10, "h", 0)
    store.playlists.set_playlist_tracks(favorite, tids[:10])
    _content_only_play(store, iid, "Folk", [0, 1])
    store.rec_model.replace_rec_discovered_content_vectors([])
    rec_params.set_param(store, "audio_transient_w", 1)
    assert _rank(store) == [cold["Techno"], cold["Folk"]]


@pytest.mark.parametrize("signal", ["play", "mood"])
def test_library_collaborative_evidence_reranks_cold_candidates(store, cold_library, signal):
    iid, _, keys, cold = cold_library
    assert _rank(store) == [cold["Folk"], cold["Techno"]]
    if signal == "play":
        store.history.record_play_event(iid, keys[0], "Techno0", 999, playlist_ytm_id="PL_owned")
    else:
        store.rec_signals.record_mood([keys[0]], 1, now=999)
    _, vectors, idx = embed.load_vectors(store)
    tilt = transient.centroid_tilt(store, 1000, vectors, idx)
    assert tilt is not None and tilt[0] > tilt[1]
    assert _rank(store) == [cold["Techno"], cold["Folk"]]


def test_library_session_evidence_reranks_cold_candidates(store, cold_library):
    iid, _, keys, cold = cold_library
    rec_params.set_param(store, "play_transient_w", 0)
    rec_params.set_param(store, "session_alpha", 1)
    rec_params.set_param(store, "now_min_events", 2)
    assert _rank(store) == [cold["Folk"], cold["Techno"]]
    for i in range(2):
        store.history.record_play_event(iid, keys[i], f"Techno{i}", 998 + i, playlist_ytm_id="PL_owned")
    assert _rank(store) == [cold["Techno"], cold["Folk"]]


def test_explicit_cold_mood_still_contributes_with_library_evidence(store, cold_library):
    _, _, _, cold = cold_library
    assert _rank(store) == [cold["Folk"], cold["Techno"]]
    store.rec_signals.record_mood([cold["Techno"]], 1, now=999)
    assert _rank(store) == [cold["Techno"], cold["Folk"]]


def test_discovery_without_metadata_is_not_surfaced(store, cold_library):
    _, _, _, cold = cold_library
    key = "unknown song|unknown artist"
    store.discovery.upsert_discovered_track(key, "unknown", "Unknown song", "Unknown artist", "", None,
                                  None, None, "MPRE_new", 10)
    candidates = surfaces.cold_candidates(store, 1000)
    assert {item.key for item in candidates} == set(cold.values())
    assert all(item.score is not None for item in candidates)


@pytest.mark.parametrize("weight", [0.5, 2.0])
def test_rankable_missing_genre_stays_neutral_against_equally_fitting_tagged_candidate(
        store, cold_library, monkeypatch, weight):
    # Both have year evidence and a genuine projection, rather than comparing a scored track to
    # the unprojectable fallback used by the old missing-metadata tests.
    class EqualProjection:
        def predict(self, *args, **kwargs):
            return np.array([1., 0.])

    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(lambda cls, s, **kwargs: EqualProjection()))
    monkeypatch.setattr(surfaces, "discovery_facet_weight", scoring.discovery_facet_weight)
    store.discovery.upsert_discovered_track("untagged|new artist", "untagged", "Untagged", "New artist", "", None,
                                  None, "2000", "MPRE_new", 10)
    store.rec_model.set_weight("genre:folk-country", 0, lo=0, hi=2, now=1000)
    store.rec_model.set_weight("genre:techno", weight, lo=0, hi=2, now=1000)
    candidates = surfaces.cold_candidates(store, 1000)
    assert len(candidates) == 2
    assert candidates[0].score == pytest.approx(candidates[1].score)
    assert all(t.score is not None for t in candidates)

    store.rec_model.set_weight("genre:techno", 0, lo=0, hi=2, now=1000)
    assert [t.key for t in surfaces.cold_candidates(store, 1000)] == ["untagged|new artist"]
