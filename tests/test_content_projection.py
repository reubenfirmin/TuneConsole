import numpy as np

from yt_playlist.rec import discover, embed, eval_recs


def test_content_projection_lands_in_right_cluster(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.set_track_genre(t, "Techno")
    for t in B:
        store.set_track_genre(t, "Folk")
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 10, "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", 10, "h2", 0.0), B)
    embed.build_and_store(store, dim=8)

    proj = discover.ContentProjection.fit(store)
    assert proj is not None
    keys, V, idx = embed.load_vectors(store)
    p = proj.predict("Techno")
    p = p / (np.linalg.norm(p) + 1e-9)
    def avg(suffix):
        return np.mean([V[idx[k]] / np.linalg.norm(V[idx[k]]) @ p for k in idx if k.endswith(suffix)])
    assert avg("|ab") > avg("|bb")              # a Techno candidate predicts into the techno cluster

    r = eval_recs.projection_recall(store, k=10)
    assert r["recall"] is not None and r["trials"] == 20


def test_projection_none_without_enough_tags(store):
    store.upsert_identity("main", "cred", None, True)
    assert discover.ContentProjection.fit(store) is None


# --- §2: enriched feature basis (audio + subgenre) and graceful degradation ---

def _two_playlists(store, A, B):
    iid = store.upsert_identity("main", "cred", None, True)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", len(A), "h", 0.0), A)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "PB", len(B), "h2", 0.0), B)
    embed.build_and_store(store, dim=8)


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
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.set_track_genre(t, "Techno"); store.set_track_year(t, "2015")
        store.set_track_audio(t, bpm=172.0, energy=0.92, danceability=0.88)
    for t in B:
        store.set_track_genre(t, "Techno"); store.set_track_year(t, "2015")
        store.set_track_audio(t, bpm=88.0, energy=0.18, danceability=0.20)
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
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.set_track_genre(t, "Deep House"); store.set_track_year(t, "2015")
    for t in B:
        store.set_track_genre(t, "Progressive House"); store.set_track_year(t, "2015")
    _two_playlists(store, A, B)

    proj = discover.ContentProjection.fit(store)
    ca, cb = _cluster_dirs(store)
    assert _toward(proj, ca, cb, genre="Deep House", year="2015") > 0
    assert _toward(proj, ca, cb, genre="Progressive House", year="2015") < 0


def test_projection_grounds_genre_only_track(store):
    # §2d graceful degradation: a projection fit on audio-rich tracks must still ground a track that has
    # only a genre (no year, no audio). predict returns a non-zero vector, never worse than the baseline.
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(10)]
    B = [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(10)]
    for t in A:
        store.set_track_genre(t, "Techno"); store.set_track_year(t, "2015")
        store.set_track_audio(t, bpm=172.0, energy=0.92, danceability=0.88)
    for t in B:
        store.set_track_genre(t, "Folk"); store.set_track_year(t, "1995")
        store.set_track_audio(t, bpm=92.0, energy=0.22, danceability=0.25)
    _two_playlists(store, A, B)

    proj = discover.ContentProjection.fit(store)
    assert proj is not None
    v = proj.predict("Techno")                       # genre only: no year, no audio passed
    assert float(np.linalg.norm(v)) > 0, "a genre-only track must still ground"
