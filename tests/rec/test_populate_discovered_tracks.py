import logging

from yt_playlist.rec import discover, embedding_vectors as embed
from tests import model_support


class _FakeClient:
    def get_album(self, browse_id):
        return {"title": "Some Album", "artists": [{"name": "New Artist"}],
                "tracks": [{"videoId": "vid1", "title": "Track One"},
                           {"videoId": "vid2", "title": "Track Two"}]}


class _Ctx:
    def __init__(self, store, client):
        self.store = store
        self.now_fn = lambda: 1.0
        self.client_provider = lambda: {1: client}
        self.logger = logging.getLogger("t")


def test_populate_discovered_tracks_builds_candidates_and_vectors(store):
    # a library track gives the content model a genre token (techno) to encode candidates into
    tid = store.tracks.upsert_track("vL", "LibT", "LibArtist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    model_support.build_content_model(store)         # persists rec_content_model
    # a discovered techno album waiting to be populated into tracks
    store.discovery.upsert_discovered_album("alb1", "New Artist", "Some Album", "2024", None, 1.0, genre="Techno")

    n = discover.populate_discovered_tracks(_Ctx(store, _FakeClient()), now=1.0)
    assert n == 2
    dtracks = store.discovery.get_discovered_tracks()
    assert {d["video_id"] for d in dtracks} == {"vid1", "vid2"}
    assert all(d["genre"] == "Techno" for d in dtracks)
    # they were encoded into the shared content space (techno token exists in the model)
    dk, DV, _ = embed.load_discovered_content_vectors(store)
    assert DV is not None and len(dk) == 2


def test_populate_skips_owned_tracks(store):
    store.tracks.set_track_genre(store.tracks.upsert_track("vL", "LibT", "LibArtist", None, None), "Techno")
    # an owned track that the album also contains, must not be re-added as out-of-corpus
    from yt_playlist.util.matching import identity_key
    store.tracks.upsert_track("vOwned", "Track One", "New Artist", None, None)
    model_support.build_content_model(store)
    store.discovery.upsert_discovered_album("alb1", "New Artist", "Some Album", "2024", None, 1.0, genre="Techno")
    discover.populate_discovered_tracks(_Ctx(store, _FakeClient()), now=1.0)
    keys = {d["identity_key"] for d in store.discovery.get_discovered_tracks()}
    assert identity_key("Track One", "New Artist") not in keys      # owned one skipped
    assert identity_key("Track Two", "New Artist") in keys


def test_expired_album_tracks_are_not_immediately_refetched(store, monkeypatch):
    monkeypatch.setattr(embed, "build_discovered_content_vectors", lambda s: None)
    store.discovery.upsert_discovered_album("alb1", "New Artist", "Some Album", "2024", None, 1.0)
    ctx = _Ctx(store, _FakeClient())
    assert discover.populate_discovered_tracks(ctx, 1.0) == 2
    assert store.discovery.gc_discovery_pool(40 * 86400, 30, set())["tracks"] == 2
    assert store.discovery.get_discovered_albums()   # album discovery has its own lifetime
    assert discover.populate_discovered_tracks(ctx, 40 * 86400) == 0
    assert store.discovery.get_discovered_tracks() == []


def test_legacy_album_tracks_do_not_refill_after_expiry(store):
    store.discovery.upsert_discovered_album("alb1", "New Artist", "Some Album", "2024", None, 1.0)
    store.discovery.upsert_discovered_track("old", "v", "Old", "New Artist", None, None, None, None, "alb1", 1)
    assert store.discovery.gc_discovery_pool(40 * 86400, 30, set())["tracks"] == 1
    assert discover.populate_discovered_tracks(_Ctx(store, _FakeClient()), 40 * 86400) == 0
