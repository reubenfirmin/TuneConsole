"""store.cluster_search: autosuggest over the user's library for seeding a cluster.

Returns vector-backed seeds across three kinds (artist / playlist / song); each result carries the
identity_keys it contributes to a node's centroid. Only keys that have a built vector are eligible:
a seed with no vector would contribute nothing to the centroid.
"""
import numpy as np

from yt_playlist.util.matching import identity_key


def _vec(store, keys):
    rows = [(k, np.ones(4, dtype=np.float32).tobytes()) for k in keys]
    store.replace_rec_vectors(rows)


def _seed_library(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # Two Ritmo tracks, one Mogwai track, all modelled; one unmodelled Ritmo track (no vector).
    r1 = store.upsert_track("r1", "Spektrum", "Ritmo", None, None)
    r2 = store.upsert_track("r2", "Pranava", "Ritmo", None, None)
    r3 = store.upsert_track("r3", "Unmodelled", "Ritmo", None, None)   # no vector -> not eligible
    m1 = store.upsert_track("m1", "Mogwai Fear Satan", "Mogwai", None, None)
    pl = store.upsert_playlist(iid, "PL1", "Late Night Drive", 2, "h", 0.0)
    store.set_playlist_tracks(pl, [r1, m1])
    _vec(store, [identity_key("Spektrum", "Ritmo"), identity_key("Pranava", "Ritmo"),
                 identity_key("Mogwai Fear Satan", "Mogwai")])
    return iid, pl


def test_artist_result_carries_all_modelled_keys(store):
    _seed_library(store)
    res = store.cluster_search("rit", limit=10)
    artist = next(r for r in res if r["kind"] == "artist" and r["label"] == "Ritmo")
    assert set(artist["keys"]) == {identity_key("Spektrum", "Ritmo"),
                                   identity_key("Pranava", "Ritmo")}   # the unmodelled one is dropped


def test_song_result_is_single_key(store):
    _seed_library(store)
    res = store.cluster_search("pranava", limit=10)
    song = next(r for r in res if r["kind"] == "song")
    assert song["keys"] == [identity_key("Pranava", "Ritmo")]
    assert song["label"] == "Pranava"


def test_playlist_result_carries_its_keys(store):
    _seed_library(store)
    res = store.cluster_search("late night", limit=10)
    pl = next(r for r in res if r["kind"] == "playlist")
    assert set(pl["keys"]) == {identity_key("Spektrum", "Ritmo"),
                               identity_key("Mogwai Fear Satan", "Mogwai")}


def test_matches_nothing_returns_empty(store):
    _seed_library(store)
    assert store.cluster_search("zzzznomatch", limit=10) == []


def test_song_ranking_prefers_exact_then_prefix(store):
    """Best match lands within the limit: an exact title beats a prefix match beats a mid-string hit,
    even when alphabetical order would bury it (an old pure-alpha ORDER BY surfaced the wrong rows)."""
    store.upsert_identity("main", "cred", None, True)
    # Alphabetically: "Acid LSD" < "LSD" < "LSD Trip" < "The LSD Song". Relevance wants "LSD" first.
    titles = ["Acid LSD", "LSD", "LSD Trip", "The LSD Song"]
    for i, title in enumerate(titles):
        store.upsert_track(f"v{i}", title, "Hallucinogen", None, None)
    _vec(store, [identity_key(t, "Hallucinogen") for t in titles])
    res = [r for r in store.cluster_search("LSD", limit=10) if r["kind"] == "song"]
    assert res[0]["label"] == "LSD"            # exact match first, despite "Acid LSD" sorting earlier
    assert res[1]["label"] == "LSD Trip"       # then the prefix match, before the mid-string hits
    # And with a tight limit the exact match still makes the cut.
    top = [r for r in store.cluster_search("LSD", limit=1) if r["kind"] == "song"]
    assert top and top[0]["label"] == "LSD"


def test_search_is_punctuation_insensitive(store):
    """#48: typing 'LSD' must find a track stored as 'L.S.D.' (and 'cafe' must find 'Café'). The
    period/accent fall out of the normalized match key, so the query needn't reproduce them."""
    store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "L.S.D.", "Hallucinogen", None, None)
    store.upsert_track("v2", "Café", "Charlotte", None, None)
    _vec(store, [identity_key("L.S.D.", "Hallucinogen"), identity_key("Café", "Charlotte")])
    songs = [r for r in store.cluster_search("LSD", limit=10) if r["kind"] == "song"]
    assert [s["label"] for s in songs] == ["L.S.D."]
    cafe = [r for r in store.cluster_search("cafe", limit=10) if r["kind"] == "song"]
    assert [c["label"] for c in cafe] == ["Café"]
