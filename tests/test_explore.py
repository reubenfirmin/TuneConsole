import numpy as np

from yt_playlist.rec import surfaces


def test_catalog_ranks_under_played_first_weighted_not_filtered(store):
    """Catalog ('explore_for_you') surfaces your under-played catalog FIRST, weighted (not filtered)
    by taste: unplayed tracks rank above played ones, and played tracks are de-ranked but NOT
    excluded (no artist filter)."""
    iid = store.identities.upsert_identity("main", "cred", None, True)
    # Both sets share an artist and equal taste fit; only play history differs.
    played = [store.tracks.upsert_track(f"p{i}", f"P{i}", "Art", None, None) for i in range(6)]
    unplayed = [store.tracks.upsert_track(f"u{i}", f"U{i}", "Art", None, None) for i in range(6)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "P", "P", 12, "h", 0.0), played + unplayed)
    for k in [f"p{i}|art" for i in range(6)]:
        store.history.record_history_plays(iid, 1.0, [k], source_timezone="UTC")
    # Truncated SVD of this symmetric catalog can choose different bases across
    # BLAS implementations. Equal vectors hold taste fit constant for this test.
    vector = np.array([1, 0, 0, 0], dtype=np.float32).tobytes()
    store.rec_model.replace_rec_vectors([(f"{prefix}{i}|art", vector)
                               for prefix in ("p", "u") for i in range(6)])

    cat = surfaces.explore_for_you(store, now=2.0, limit=12)
    assert len(cat) == 12, "played tracks are de-ranked, not excluded"
    assert all(i.title.startswith("U") for i in cat[:6])
    assert all(i.title.startswith("P") for i in cat[6:])
    assert all(i.lane == "explore" for i in cat)


def test_explore_empty_without_vectors(store):
    store.identities.upsert_identity("main", "cred", None, True)
    assert surfaces.explore_for_you(store, now=1.0) == []
