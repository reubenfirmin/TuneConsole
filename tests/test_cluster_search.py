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
