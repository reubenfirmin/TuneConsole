"""#28 artist-relationship model: §A collaborative embedding (Task 1)."""
from yt_playlist.rec import artist_model
from tests import model_support


def _playlist(s, iid, title, track_ids):
    pid = s.playlists.upsert_playlist(iid, title, title, len(track_ids), "h", 0.0)
    s.playlists.set_playlist_tracks(pid, track_ids)
    return pid


def test_artist_baskets_reduce_to_distinct_artists(store):
    s = store
    iid = s.accounts.upsert_account("m", "c", None, True)
    a = s.tracks.upsert_track("v1", "S1", "Alpha", None, None)
    b = s.tracks.upsert_track("v2", "S2", "Beta", None, None)
    _playlist(s, iid, "P", [a, b])
    baskets = artist_model.artist_baskets(s)
    assert any({s.artists.key('Alpha'), s.artists.key('Beta')} <= set(bk) for bk in baskets)
    assert all(len(set(bk)) == len(bk) for bk in baskets)        # distinct artists per basket
    assert all(len(bk) >= 2 for bk in baskets)                   # single-artist baskets dropped (no self-relate)


def test_co_curated_ranking_and_seed_exclusion_work_without_content_vectors(store):
    """One collaborative fit owns neighbor ranking, missing-content fallback and set scoring."""
    from yt_playlist.util.matching import normalize
    s = store
    iid = s.accounts.upsert_account("m", "c", None, True)

    def mk(name):
        return s.tracks.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    clusterA = [mk(f"A{i}") for i in range(6)]
    clusterB = [mk(f"B{i}") for i in range(6)]
    for p in range(3):                      # repeated co-occurrence within each cluster
        _playlist(s, iid, f"PA{p}", clusterA)
        _playlist(s, iid, f"PB{p}", clusterB)

    n = model_support.build_collaborative_artist_model(s, dim=4)
    assert n >= 12
    assert artist_model.load_artist_content_vectors(s)[1] is None
    aset = {normalize(f"A{i}") for i in range(6)}
    nbrs = [a for a, _ in artist_model.artist_neighbors(s, "A0", topn=4)]
    assert nbrs
    assert all(a in aset for a in nbrs)     # cluster-mates rank above the other cluster
    rel = artist_model.related_artist_scores(s, ["A0", "A1"], topn=4, exclude_owned=False)
    assert rel and all(a in {s.artists.key(f"A{i}") for i in range(6)} for a in rel)
    assert s.artists.key("A0") not in rel and s.artists.key("A1") not in rel
    assert artist_model.related_artist_scores(s, ["A0", "A1"], topn=4) == {}


def test_neighbors_empty_when_unbuilt(store):
    s = store
    assert artist_model.artist_neighbors(s, "Anyone") == []


def test_content_places_out_of_corpus_artist(store):
    """#28 §B: an out-of-corpus artist with only a genre still encodes into the shared artist-content
    space and has positive cosine to a same-genre library artist (graceful placement)."""
    import numpy as np
    from yt_playlist.util import genre_map
    s = store
    s.accounts.upsert_account("m", "c", None, True)
    t = s.tracks.upsert_track("v1", "S1", "Owned", None, None)
    s.tracks.set_track_genre(t, "Techno")
    profiles = artist_model.artist_content_profiles(s)
    model = artist_model.build_artist_content_model(profiles)
    owned = artist_model.encode_artist_content(model, profiles[s.artists.key('Owned')])
    oop = artist_model.encode_artist_content(model, {"families": {genre_map.family("Techno")}})
    assert owned is not None and oop is not None
    assert float(oop @ owned) > 0.0      # same family -> related in the content space


def test_build_persists_and_loads(store):
    """#28 Both collaborative and content artist fixtures use the persisted format."""
    s = store
    iid = s.accounts.upsert_account("m", "c", None, True)
    cl = []
    for i in range(12):
        t = s.tracks.upsert_track(f"v{i}", f"S{i}", f"A{i}", None, None)
        s.tracks.set_track_genre(t, "Techno")
        cl.append(t)
    for p in range(3):
        _playlist(s, iid, f"P{p}", cl)
    model_support.build_artist_model(s, dim=4)
    a_artists, AV, _ = artist_model.load_artist_vectors(s)
    c_artists, CV, _ = artist_model.load_artist_content_vectors(s)
    assert AV is not None and len(a_artists) >= 12      # collaborative vectors persisted
    assert CV is not None and len(c_artists) >= 12      # content vectors persisted


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


def test_complete_playlist_pulls_out_of_corpus(store):
    """#24: 'Complete this playlist' includes out-of-corpus tracks by artists related to the playlist's
    artists, labeled, and never re-suggests the playlist's own tracks."""
    from yt_playlist.rec import surfaces
    from yt_playlist.util.matching import normalize
    s = store
    iid = s.accounts.upsert_account("m", "c", None, True)

    def mk(name):
        return s.tracks.upsert_track(f"v_{name}", f"S_{name}", name, None, None)

    cluster = [mk(f"A{i}") for i in range(10)]
    for p in range(3):
        _playlist(s, iid, f"PA{p}", cluster)
    target = s.playlists.upsert_playlist(iid, "TARGET", "t", 1, "h", 0.0)
    s.playlists.set_playlist_tracks(target, [cluster[0]])               # playlist of just A0
    s.tracks.set_track_genre(cluster[0], "Techno")
    model_support.build_collaborative_artist_model(s, dim=4)
    s.artist_similarity.cache_similar(s.artists.key("A0"), [["Gamma", 0.9]], now=1.0)
    # The provider edge must work before Gamma has any track or vector.
    assert normalize("Gamma") not in artist_model.load_artist_vectors(s)[0]
    assert artist_model.load_artist_content_vectors(s)[1] is None
    nbrs = [a for a, _ in artist_model.artist_neighbors(s, "A0", topn=10)]
    assert normalize("Gamma") in nbrs
    s.discovery.upsert_discovered_track("song x|gamma", "vg", "Song X", "Gamma", "", None, "Techno", None, "src", 1.0)
    cands = artist_model.artist_track_candidates(s, ["A0"], topn=10)
    assert normalize("Gamma") in {normalize(c["artist"]) for c in cands}
    assert any(c.get("out_of_corpus") for c in cands)

    sugg = surfaces.complete_playlist(s, target, now=1.0)
    keys = {x.key for x in sugg}
    assert "song x|gamma" in keys                            # out-of-corpus pull surfaced
    g = next(x for x in sugg if x.key == "song x|gamma")
    assert g.lane == "related_artist" and g.reason == "New · Related artist · shared techno genre"
    assert not (keys & set(s.playlists.get_playlist_track_keys(target)))   # never the playlist's own tracks
