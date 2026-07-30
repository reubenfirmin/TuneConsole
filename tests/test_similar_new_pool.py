"""#105 "Songs like this" only ever offered tracks you already own.

The collaborative space is built from playlist co-occurrence baskets, so by construction it contains
only tracks that are already in your playlists: the out-of-corpus discovered pool cannot appear in
it, no matter how close a match. The content space (genre/era/audio) is where that pool lives, so the
seed's neighbours are now merged across both.
"""
from fastapi.testclient import TestClient

from yt_playlist.rec import embed
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

SEED = "painkiller|judas priest"


def test_blend_interleaves_both_spaces_collaborative_first(monkeypatch):
    """Merged by rank, not score: the two spaces are different metrics whose numbers do not compare,
    but their Nth picks do. Collaborative leads, being the stronger signal for tracks you own."""
    monkeypatch.setattr(embed, "neighbors", lambda s, k, topn=12: [("lib1", 0.9), ("lib2", 0.8)])
    monkeypatch.setattr(embed, "_content_space", lambda s, include_new: (
        [SEED, "new1", "new2"], _eye(3), {SEED: 0, "new1": 1, "new2": 2}))
    got = [k for k, _ in embed.neighbors_blended(None, SEED, topn=4)]
    assert got[0] == "lib1"                       # collaborative leads
    assert set(got) == {"lib1", "lib2", "new1", "new2"}


def test_blend_never_repeats_a_track_present_in_both_spaces(monkeypatch):
    monkeypatch.setattr(embed, "neighbors", lambda s, k, topn=12: [("dup", 0.9)])
    monkeypatch.setattr(embed, "_content_space", lambda s, include_new: (
        [SEED, "dup"], _eye(2), {SEED: 0, "dup": 1}))
    assert [k for k, _ in embed.neighbors_blended(None, SEED, topn=4)] == ["dup"]


def test_blend_asks_for_the_widened_space(monkeypatch):
    """The pool only exists in the content space when include_new is set: without it this is just a
    slower way to return the same catalogue."""
    seen = {}

    def fake_space(store, include_new):
        seen["include_new"] = include_new
        return ([], None, {})

    monkeypatch.setattr(embed, "_content_space", fake_space)
    monkeypatch.setattr(embed, "neighbors", lambda s, k, topn=12: [])
    embed.neighbors_blended(None, SEED, topn=4)
    assert seen["include_new"] is True


def _eye(n):
    import numpy as np
    return np.eye(n, dtype="float32")


def test_modal_renders_a_neighbour_that_only_exists_in_the_new_pool(store, monkeypatch):
    """The end of the chain: a pool track is not in `tracks`, so resolving neighbours against the
    library alone silently dropped it even once the blend surfaced it."""
    iid = store.upsert_identity("main", "cred", None, True)
    tid = store.upsert_track("seedvid", "Painkiller", "Judas Priest", "Painkiller", 300)
    store.upsert_discovered_track("overkill|motorhead", "ovk1", "Overkill", "Motorhead",
                                  "Overkill (Expanded Edition)", "http://t/o.jpg", "metal", "1979",
                                  "MPREb_z", 1000.0)
    seed_key = store.identity_key_for_video("seedvid")
    monkeypatch.setattr(embed, "neighbors_blended",
                        lambda s, k, topn=12: [("overkill|motorhead", 0.91)])
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                   base_url="http://127.0.0.1")
    html = c.get("/track/seedvid/similar").text
    assert "Overkill" in html and "Motorhead" in html
    assert "ovk1" in html            # the Play link + the add-to-playlist payload both need the vid
    assert tid is not None and seed_key
