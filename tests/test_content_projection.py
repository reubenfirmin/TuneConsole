import numpy as np
import pytest

from yt_playlist.rec import discover, embed, eval_recs
from tests import model_support


def test_content_projection_lands_in_right_cluster(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(15)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(15)]
    for t in A:
        store.tracks.set_track_genre(t, "Techno")
    for t in B:
        store.tracks.set_track_genre(t, "Folk")
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", 10, "h", 0.0), A)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "PB", 10, "h2", 0.0), B)
    model_support.build_track_model(store, dim=8)

    proj = discover.ContentProjection.fit(store)
    assert proj is not None
    keys, V, idx = embed.load_vectors(store)
    p = proj.predict("Techno")
    p = p / (np.linalg.norm(p) + 1e-9)
    def avg(suffix):
        return np.mean([V[idx[k]] / np.linalg.norm(V[idx[k]]) @ p for k in idx if k.endswith(suffix)])
    assert avg("|ab") > avg("|bb")              # a Techno candidate predicts into the techno cluster

    r = eval_recs.projection_recall(store, k=10)
    assert r["recall"] is not None and r["trials"] == 30
    assert r["split"] == "track_identity_5fold" and not r["in_sample"]


def test_projection_none_without_enough_tags(store):
    store.identities.upsert_identity("main", "cred", None, True)
    assert discover.ContentProjection.fit(store) is None


def test_evaluation_excludes_targets_and_feature_fitting_inputs(store, monkeypatch):
    keys = []
    for i in range(30):
        tid = store.tracks.upsert_track(f"v{i}", f"Track {i}", "Artist", None, None)
        store.tracks.set_track_genre(tid, "Techno")
        store.tracks.set_track_year(tid, str(1990 + i))
        store.tracks.set_track_audio(tid, bpm=100.0 + i)
        keys.append(f"track {i}|artist")
    vectors = np.eye(30, dtype=np.float32)
    monkeypatch.setattr(embed, "load_vectors", lambda s: (keys, vectors, {k: i for i, k in enumerate(keys)}))
    original_fit = discover.ContentProjection.fit
    original_model = embed.build_content_model
    original_solve = np.linalg.solve
    active_training = set()
    tested = []

    def model(content, audio):
        assert set(content) == active_training
        assert set(audio) == active_training
        return original_model(content, audio)

    def solve(lhs, rhs):
        # Orthogonal reference targets make any held-out row entering Y observable here.
        for i, key in enumerate(keys):
            if key not in active_training:
                assert np.all(rhs[:, i] == 0)
        return original_solve(lhs, rhs)

    def fit(cls, s, *, training_keys, include_artist):
        nonlocal active_training
        assert not include_artist
        active_training = set(training_keys)
        assert len(active_training) == 24
        projection = original_fit(s, training_keys=training_keys, include_artist=include_artist)
        predict = projection.predict

        def held_out(genre, year, audio):
            key = f"track {int(year) - 1990}|artist"
            assert key not in active_training
            tested.append(key)
            return predict(genre, year, audio)

        projection.predict = held_out
        return projection

    monkeypatch.setattr(embed, "build_content_model", model)
    monkeypatch.setattr(np.linalg, "solve", solve)
    monkeypatch.setattr(discover.artist_model, "load_artist_vectors",
                        lambda s: pytest.fail("evaluation must not use shared artist features"))
    monkeypatch.setattr(discover.ContentProjection, "fit", classmethod(fit))
    result = eval_recs.projection_recall(store, k=5)
    assert sorted(tested) == sorted(keys)
    assert result["eligible"] == result["trials"] == 30
    assert result["insufficient"] == result["unscored"] == 0


def test_projection_reports_insufficient_fold_evidence(store, monkeypatch):
    keys = []
    for i in range(20):
        tid = store.tracks.upsert_track(f"v{i}", f"Track {i}", "Artist", None, None)
        store.tracks.set_track_genre(tid, "Techno")
        keys.append(f"track {i}|artist")
    monkeypatch.setattr(embed, "load_vectors",
                        lambda s: (keys, np.eye(20), {k: i for i, k in enumerate(keys)}))
    result = eval_recs.projection_recall(store)
    assert result["recall"] is None and result["trials"] == 0
    assert result["insufficient"] == result["eligible"] == 20
    assert result["reason"]


# --- §2: enriched feature basis (audio + subgenre) and graceful degradation ---

def _two_playlists(store, A, B):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "PA", len(A), "h", 0.0), A)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "PB", len(B), "h2", 0.0), B)
    model_support.build_track_model(store, dim=8)


def _cluster_dirs(store):
    """Unit collaborative centroids of the two clusters ('|ab' and '|bb' identity-key suffixes)."""
    keys, V, idx = embed.load_vectors(store)

    def centroid(suffix):
        c = np.mean([V[idx[k]] / np.linalg.norm(V[idx[k]]) for k in idx if k.endswith(suffix)], axis=0)
        return c / (np.linalg.norm(c) + 1e-9)

    return centroid("|ab"), centroid("|bb")


def _toward(proj, ca, cb, **content):
    """How much a predicted content vector leans to cluster A vs B: positive means it grounds into A."""
    p = proj.predict(content.get("genre"), content.get("year"), content.get("audio"))
    p = p / (np.linalg.norm(p) + 1e-9)
    return float(p @ ca - p @ cb)


def test_projection_uses_audio_to_separate_same_genre_year(store):
    # Two clusters with IDENTICAL genre + year, separable ONLY by audio (tempo / energy), in separate
    # playlists so the collaborative embedding pulls them apart. Genre+year alone predicts ONE vector for
    # both, so it cannot lean a candidate toward its own cluster. Audio must. (Genre+year-only would make
    # the two predictions identical, so the strict inequalities below fail without the audio block.)
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.tracks.set_track_genre(t, "Techno"); store.tracks.set_track_year(t, "2015")
        store.tracks.set_track_audio(t, bpm=172.0, energy=0.92, danceability=0.88)
    for t in B:
        store.tracks.set_track_genre(t, "Techno"); store.tracks.set_track_year(t, "2015")
        store.tracks.set_track_audio(t, bpm=88.0, energy=0.18, danceability=0.20)
    _two_playlists(store, A, B)

    proj = discover.ContentProjection.fit(store)
    ca, cb = _cluster_dirs(store)
    fast = {"genre": "Techno", "year": "2015", "audio": {"bpm": 172.0, "energy": 0.92, "danceability": 0.88}}
    slow = {"genre": "Techno", "year": "2015", "audio": {"bpm": 88.0, "energy": 0.18, "danceability": 0.20}}
    assert _toward(proj, ca, cb, **fast) > 0, "high-energy audio should ground into cluster A"
    assert _toward(proj, ca, cb, **slow) < 0, "low-energy audio should ground into cluster B"


def test_projection_uses_subgenre_to_separate_same_family(store):
    # Same family (house), different subgenre, no audio. Family-only predicts one vector per family and
    # cannot separate Deep House from Progressive House; the subgenre feature must.
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.tracks.set_track_genre(t, "Deep House"); store.tracks.set_track_year(t, "2015")
    for t in B:
        store.tracks.set_track_genre(t, "Progressive House"); store.tracks.set_track_year(t, "2015")
    _two_playlists(store, A, B)

    proj = discover.ContentProjection.fit(store)
    ca, cb = _cluster_dirs(store)
    assert _toward(proj, ca, cb, genre="Deep House", year="2015") > 0
    assert _toward(proj, ca, cb, genre="Progressive House", year="2015") < 0


def test_projection_grounds_genre_only_track(store):
    # §2d graceful degradation: a projection fit on audio-rich tracks must still ground a track that has
    # only a genre (no year, no audio). predict returns a non-zero vector, never worse than the baseline.
    A = [store.tracks.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.tracks.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.tracks.set_track_genre(t, "Techno"); store.tracks.set_track_year(t, "2015")
        store.tracks.set_track_audio(t, bpm=172.0, energy=0.92, danceability=0.88)
    for t in B:
        store.tracks.set_track_genre(t, "Folk"); store.tracks.set_track_year(t, "1995")
        store.tracks.set_track_audio(t, bpm=92.0, energy=0.22, danceability=0.25)
    _two_playlists(store, A, B)

    proj = discover.ContentProjection.fit(store)
    assert proj is not None
    v = proj.predict("Techno")                       # genre only: no year, no audio passed
    assert float(np.linalg.norm(v)) > 0, "a genre-only track must still ground"
