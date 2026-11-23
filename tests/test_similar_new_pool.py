"""#105 "Songs like this" only ever offered tracks you already own.

The collaborative space is built from playlist co-occurrence baskets, so by construction it contains
only tracks that are already in your playlists: the out-of-corpus discovered pool cannot appear in
it, no matter how close a match. The content space (genre/era/audio) is where that pool lives, so the
seed's neighbours are now merged across both.
"""
import re

from fastapi.testclient import TestClient

from yt_playlist.rec import embed, embedding_vectors as vectors
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

SEED = "painkiller|judas priest"


def test_blend_interleaves_both_spaces_collaborative_first(monkeypatch):
    """Merged by rank, not score: the two spaces are different metrics whose numbers do not compare,
    but their Nth picks do. Collaborative leads, being the stronger signal for tracks you own."""
    monkeypatch.setattr(embed, "neighbors", lambda s, k, topn=12: [("lib1", 0.9), ("lib2", 0.8)])
    monkeypatch.setattr(vectors, "content_space", lambda s, include_new: (
        [SEED, "new1", "new2"], _eye(3), {SEED: 0, "new1": 1, "new2": 2}))
    got = [k for k, _ in embed.neighbors_blended(None, SEED, topn=4)]
    assert got[0] == "lib1"                       # collaborative leads
    assert set(got) == {"lib1", "lib2", "new1", "new2"}


def test_blend_never_repeats_a_track_present_in_both_spaces(monkeypatch):
    monkeypatch.setattr(embed, "neighbors", lambda s, k, topn=12: [("dup", 0.9)])
    monkeypatch.setattr(vectors, "content_space", lambda s, include_new: (
        [SEED, "dup"], _eye(2), {SEED: 0, "dup": 1}))
    assert [k for k, _ in embed.neighbors_blended(None, SEED, topn=4)] == ["dup"]


def test_blend_asks_for_the_widened_space(monkeypatch):
    """The pool only exists in the content space when include_new is set: without it this is just a
    slower way to return the same catalogue."""
    seen = {}

    def fake_space(store, include_new):
        seen["include_new"] = include_new
        return ([], None, {})

    monkeypatch.setattr(vectors, "content_space", fake_space)
    monkeypatch.setattr(embed, "neighbors", lambda s, k, topn=12: [])
    embed.neighbors_blended(None, SEED, topn=4)
    assert seen["include_new"] is True


def _eye(n):
    import numpy as np
    return np.eye(n, dtype="float32")


def test_modal_renders_a_neighbour_that_only_exists_in_the_new_pool(store, monkeypatch):
    """The end of the chain: a pool track is not in `tracks`, so resolving neighbours against the
    library alone silently dropped it even once the blend surfaced it."""
    iid = store.accounts.upsert_account("main", None, True)
    tid = store.tracks.upsert_track("seedvid", "Painkiller", "Judas Priest", "Painkiller", 300)
    store.discovery.upsert_discovered_track("overkill|motorhead", "ovk1", "Overkill", "Motorhead",
                                  "Overkill (Expanded Edition)", "http://t/o.jpg", "metal", "1979",
                                  "MPREb_z", 1000.0)
    seed_key = store.tracks.identity_key_for_video("seedvid")
    monkeypatch.setattr(embed, "neighbors_blended",
                        lambda s, k, topn=12: [("overkill|motorhead", 0.91)])
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                   base_url="http://127.0.0.1")
    html = c.get("/track/seedvid/similar").text
    assert "Overkill" in html and "Motorhead" in html
    assert "ovk1" in html            # the Play link + the add-to-playlist payload both need the vid
    assert tid is not None and seed_key


def test_similar_modal_uses_an_available_discovery_upload_or_skips_the_recording(store, monkeypatch):
    account = store.accounts.upsert_account('main', None, True)
    store.tracks.upsert_track('seed-video', 'Seed', 'Seed Artist', None, 240)
    preferred, other = [store.discovery.upsert_discovered_track(
        f'{title.lower()}|artist', video, title, 'Artist', None, None, 'Metal', None, None, 1000.)
        for video, title in [('preferred-video', 'Favorite'), ('other-video', 'Other')]]
    representative = store.discovery.discovered_tracks_by_keys([preferred])[preferred]
    store.tracks.upsert_track('alternate-video', 'Favorite (alternate upload)', 'Artist', library=False)
    store.tracks.associate_youtube_item('alternate-video', representative['recording_id'], source='verified',
                                        evidence={'isrc': 'same recording'}, now=1000., same_recording=True)
    monkeypatch.setattr(embed, 'track_neighbors', lambda store, key, topn: [(preferred, .9), (other, .8)])
    client = TestClient(create_app(store, lambda: {account: FakeClient()}, now_fn=lambda: 1000.),
                        base_url='http://127.0.0.1')

    def offered_uploads():
        response = client.get('/track/seed-video/similar')
        assert response.status_code == 200
        return re.findall(r'music\.youtube\.com/watch\?v=([^"&]+)', response.text)

    assert offered_uploads() == ['preferred-video', 'other-video']
    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='preferred-video'")
    assert offered_uploads() == ['alternate-video', 'other-video']
    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='alternate-video'")
    assert offered_uploads() == ['other-video']


def test_similar_modal_uses_artist_credits_when_the_seed_has_no_vectors(store):
    import numpy as np

    iid = store.accounts.upsert_account("main", None, True)
    store.tracks.upsert_track("seedvid", "Seed Song", "Shared Artist", None, 240)
    store.tracks.upsert_track("neighborvid", "Modeled Neighbor", "Shared Artist", None, 240)
    neighbor = store.tracks.identity_key_for_video("neighborvid")
    store.rec_model.replace_rec_vectors([(neighbor, np.ones(1, dtype=np.float32).tobytes())])
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                        base_url="http://127.0.0.1")
    response = client.get("/track/seedvid/similar")
    assert response.status_code == 200
    assert "Modeled Neighbor" in response.text
