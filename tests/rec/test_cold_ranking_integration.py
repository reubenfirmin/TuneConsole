"""#50: persisted discoveries and library evidence exercise the real cold scoring pipeline."""
import numpy as np
import pytest

from yt_playlist.rec import discover, embed, eval_recs, rec_params, scoring, surfaces, transient
from yt_playlist.util.matching import identity_key


@pytest.fixture
def cold_library(store, monkeypatch):
    # The separate genre overlay can reorder these same candidates, masking a broken vector
    # tilt. Hold that overlay neutral; projection, taste and both tilt producers stay real.
    monkeypatch.setattr(surfaces, "discovery_facet_weight", lambda *args: 1.0)
    iid = store.identities.upsert_identity("main", "cred", None, True)
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
    iid, tids, _, cold = cold_library
    favorite = store.playlists.upsert_playlist(iid, "PL_favorite", "Favorite sound", 10, "h", 0)
    store.playlists.set_playlist_tracks(favorite, tids[:10])
    _, vectors, idx = embed.load_vectors(store)
    taste = scoring.playlist_taste(store)
    before = scoring._apply_mood(taste.score_all(vectors), store, 1000, vectors, idx)
    assert _rank(store) == [cold["Techno"], cold["Folk"]]
    np.testing.assert_array_equal(before, scoring._apply_mood(taste.score_all(vectors), store, 1000, vectors, idx))
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
        store.rec_surface.record_mood([keys[0]], 1, now=999)
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
    store.rec_surface.record_mood([cold["Techno"]], 1, now=999)
    assert _rank(store) == [cold["Techno"], cold["Folk"]]


def test_discovery_without_metadata_is_not_surfaced(store, cold_library):
    key = "unknown song|unknown artist"
    store.discovery.upsert_discovered_track(key, "unknown", "Unknown song", "Unknown artist", "", None,
                                  None, None, "MPRE_new", 10)
    assert key not in _rank(store)
    assert eval_recs.cold_rankable(store, 1000) == {
        "rankable": 2, "fallback": 0, "eligible": 2, "pool": 3}


@pytest.mark.parametrize("weight", [0.5, 2.0])
def test_rankable_missing_genre_stays_neutral_against_equally_fitting_tagged_candidate(
        store, cold_library, monkeypatch, weight):
    # Both have year evidence and a genuine projection, rather than comparing a scored track to
    # the unprojectable fallback used by the old missing-metadata tests.
    class EqualProjection:
        def predict(self, *args, **kwargs):
            return np.array([1., 0.])

    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(lambda cls, s: EqualProjection()))
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
