"""#28 artist-relationship model: §A collaborative embedding (Task 1)."""
from yt_playlist.rec import artist_model
from yt_playlist.core.store import Store


def _store():
    s = Store(":memory:")
    s.init_schema()
    return s


def _playlist(s, iid, title, track_ids):
    pid = s.upsert_playlist(iid, title, title, len(track_ids), "h", 0.0)
    s.set_playlist_tracks(pid, track_ids)
    return pid


def test_artist_baskets_reduce_to_distinct_artists():
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)
    a = s.upsert_track("v1", "S1", "Alpha", None, None)
    b = s.upsert_track("v2", "S2", "Beta", None, None)
    _playlist(s, iid, "P", [a, b])
    baskets = artist_model.artist_baskets(s)
    assert any({"alpha", "beta"} <= set(bk) for bk in baskets)   # a playlist basket relates the two artists
    assert all(len(set(bk)) == len(bk) for bk in baskets)        # distinct artists per basket
    assert all(len(bk) >= 2 for bk in baskets)                   # single-artist baskets dropped (no self-relate)


def test_collab_neighbors_relate_co_curated_artists():
    """Two clean co-curation clusters: A0..A4 always together, B0..B4 always together, never crossed.
    A0's nearest collaborative neighbours are its own cluster-mates."""
    from yt_playlist.util.matching import normalize
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)

    def mk(name):
        return s.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    clusterA = [mk(f"A{i}") for i in range(5)]
    clusterB = [mk(f"B{i}") for i in range(5)]
    for p in range(3):                      # repeated co-occurrence within each cluster
        _playlist(s, iid, f"PA{p}", clusterA)
        _playlist(s, iid, f"PB{p}", clusterB)

    n = artist_model.build_collab_and_store(s, dim=4)
    assert n >= 10
    aset = {normalize(f"A{i}") for i in range(5)}
    nbrs = [a for a, _ in artist_model.artist_neighbors(s, "A0", topn=4)]
    assert nbrs
    assert all(a in aset for a in nbrs)     # cluster-mates rank above the other cluster


def test_neighbors_empty_when_unbuilt():
    s = _store()
    assert artist_model.artist_neighbors(s, "Anyone") == []


def test_artist_recall_beats_baseline():
    """#28 artist-level recall@k on two clean clusters: holding out one cluster artist, the rest's
    centroid ranks it near the top, well above the random baseline."""
    from yt_playlist.rec import eval_recs
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)

    def mk(name):
        return s.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    clusterA = [mk(f"A{i}") for i in range(6)]
    clusterB = [mk(f"B{i}") for i in range(6)]
    for p in range(3):
        _playlist(s, iid, f"PA{p}", clusterA)
        _playlist(s, iid, f"PB{p}", clusterB)
    artist_model.build_collab_and_store(s, dim=4)

    res = eval_recs.artist_recall_at_k(s, k=3, min_size=3)
    assert res["recall_at_k"] is not None and res["trials"] > 0
    assert res["recall_at_k"] > res["baseline"]      # the model beats random
    assert res["lift"] and res["lift"] > 1.0


def test_content_places_out_of_corpus_artist():
    """#28 §B: an out-of-corpus artist with only a genre still encodes into the shared artist-content
    space and has positive cosine to a same-genre library artist (graceful placement)."""
    import numpy as np
    from yt_playlist.util import genre_map
    s = _store()
    s.upsert_identity("m", "c", None, True)
    t = s.upsert_track("v1", "S1", "Owned", None, None)
    s.set_track_genre(t, "Techno")
    profiles = artist_model.artist_content_profiles(s)
    model = artist_model.build_artist_content_model(profiles)
    owned = artist_model.encode_artist_content(model, profiles["owned"])
    oop = artist_model.encode_artist_content(model, {"families": {genre_map.family("Techno")}})
    assert owned is not None and oop is not None
    assert float(oop @ owned) > 0.0      # same family -> related in the content space


def test_blend_falls_back_to_collab_when_no_content():
    """With no content vectors built, artist_neighbors still works on §A alone (blend degrades)."""
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)

    def mk(name):
        return s.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    clusterA = [mk(f"A{i}") for i in range(6)]
    clusterB = [mk(f"B{i}") for i in range(6)]
    for p in range(3):
        _playlist(s, iid, f"PA{p}", clusterA)
        _playlist(s, iid, f"PB{p}", clusterB)
    artist_model.build_collab_and_store(s, dim=4)        # §A only, no content built
    nbrs = [a for a, _ in artist_model.artist_neighbors(s, "A0", topn=4)]
    from yt_playlist.util.matching import normalize
    assert nbrs and all(a in {normalize(f"A{i}") for i in range(6)} for a in nbrs)


def test_edges_connect_out_of_corpus_artist():
    """#28 §C: a cached Last.fm edge to an artist with no §A/§B vector still surfaces it as related."""
    from yt_playlist.util.matching import normalize
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)

    def mk(name):
        return s.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    clusterA = [mk(f"A{i}") for i in range(6)]
    for p in range(3):
        _playlist(s, iid, f"PA{p}", clusterA)
    artist_model.build_collab_and_store(s, dim=4)
    # Gamma is out of corpus (no track, no vector) but Last.fm relates A0 -> Gamma strongly.
    s.cache_similar(normalize("A0"), [["Gamma", 0.9]], now=1.0)
    nbrs = [a for a, _ in artist_model.artist_neighbors(s, "A0", topn=10)]
    assert normalize("Gamma") in nbrs        # edge-only candidate is reachable


def test_build_persists_and_loads():
    """#28 build_artist_model_and_store persists both §A and §B vectors so they reload."""
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)
    cl = []
    for i in range(12):
        t = s.upsert_track(f"v{i}", f"S{i}", f"A{i}", None, None)
        s.set_track_genre(t, "Techno")
        cl.append(t)
    for p in range(3):
        _playlist(s, iid, f"P{p}", cl)
    artist_model.build_artist_model_and_store(s, dim=4)
    a_artists, AV, _ = artist_model.load_artist_vectors(s)
    c_artists, CV, _ = artist_model.load_artist_content_vectors(s)
    assert AV is not None and len(a_artists) >= 12      # collaborative vectors persisted
    assert CV is not None and len(c_artists) >= 12      # content vectors persisted


def test_related_artists_from_set():
    """related_artists over a SET of seeds returns their co-curation cluster-mates (exclude_owned=False
    keeps owned related artists, as 'complete this playlist' needs)."""
    from yt_playlist.util.matching import normalize
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)

    def mk(name):
        return s.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    clusterA = [mk(f"A{i}") for i in range(6)]
    clusterB = [mk(f"B{i}") for i in range(6)]
    for p in range(3):
        _playlist(s, iid, f"PA{p}", clusterA)
        _playlist(s, iid, f"PB{p}", clusterB)
    artist_model.build_collab_and_store(s, dim=4)
    rel = [a for a, _ in artist_model.related_artists(s, ["A0", "A1"], topn=4, exclude_owned=False)]
    aset = {normalize(f"A{i}") for i in range(6)}
    assert rel and all(a in aset for a in rel)
    assert normalize("A0") not in rel and normalize("A1") not in rel   # seeds excluded


def test_track_candidates_include_out_of_corpus():
    """artist_track_candidates pulls an out-of-corpus discovered track by an edge-related artist."""
    from yt_playlist.util.matching import normalize
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)

    def mk(name):
        return s.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    clusterA = [mk(f"A{i}") for i in range(6)]
    for p in range(3):
        _playlist(s, iid, f"PA{p}", clusterA)
    artist_model.build_collab_and_store(s, dim=4)
    s.cache_similar(normalize("A0"), [["Gamma", 0.9]], now=1.0)        # A0 -> Gamma edge
    s.upsert_discovered_track("song x|gamma", "vg", "Song X", "Gamma", "", None, "Techno", None, "src", 1.0)
    cands = artist_model.artist_track_candidates(s, ["A0"], topn=10)
    arts = {normalize(c["artist"]) for c in cands}
    assert normalize("Gamma") in arts
    assert any(c.get("out_of_corpus") for c in cands)


def test_content_projection_grounds_on_artist_when_no_content():
    """#28 §2c: a track with no genre/audio still grounds via its (known) artist's vector; an unknown
    artist degrades to a zero block. Tests the ContentProjection feature seam directly."""
    import numpy as np
    from yt_playlist.rec.discover import ContentProjection
    empty_model = {"cat": {}, "ncat": 0, "cont": []}        # content block is zero-length
    AV = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    proj = ContentProjection(empty_model, None, {"alpha": 0}, AV)
    f_known = proj._features(None, None, None, "Alpha")     # known artist -> artist block set
    f_unknown = proj._features(None, None, None, "Nobody")  # unknown artist -> zeros
    assert f_known.shape == (3,) and np.any(f_known)
    assert not np.any(f_unknown)


def test_related_artist_suggestions_pulls_out_of_corpus():
    """#24: 'Complete this playlist' includes out-of-corpus tracks by artists related to the playlist's
    artists, labeled, and never re-suggests the playlist's own tracks."""
    from yt_playlist.rec import recommend
    from yt_playlist.util.matching import normalize
    s = _store()
    iid = s.upsert_identity("m", "c", None, True)

    def mk(name):
        return s.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    cluster = [mk(f"A{i}") for i in range(10)]
    for p in range(3):
        _playlist(s, iid, f"PA{p}", cluster)
    target = s.upsert_playlist(iid, "TARGET", "t", 1, "h", 0.0)
    s.set_playlist_tracks(target, [cluster[0]])               # playlist of just A0
    artist_model.build_collab_and_store(s, dim=4)
    s.cache_similar(normalize("A0"), [["Gamma", 0.9]], now=1.0)
    s.upsert_discovered_track("song x|gamma", "vg", "Song X", "Gamma", "", None, "Techno", None, "src", 1.0)

    sugg = recommend.related_artist_suggestions(s, target, now=1.0)
    keys = {x.key for x in sugg}
    assert "song x|gamma" in keys                            # out-of-corpus pull surfaced
    g = next(x for x in sugg if x.key == "song x|gamma")
    assert g.lane == "related_artist" and g.reason.startswith("New:")
    assert not (keys & set(s.get_playlist_track_keys(target)))   # never the playlist's own tracks
