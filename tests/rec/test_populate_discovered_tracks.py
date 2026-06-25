import logging

from yt_playlist.rec import discover, embed


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
    tid = store.upsert_track("vL", "LibT", "LibArtist", None, None)
    store.set_track_genre(tid, "Techno")
    embed.build_content_and_store(store)             # persists rec_content_model
    # a discovered techno album waiting to be populated into tracks
    store.upsert_discovered_album("alb1", "New Artist", "Some Album", "2024", None, 1.0, genre="Techno")

    n = discover.populate_discovered_tracks(_Ctx(store, _FakeClient()), now=1.0)
    assert n == 2
    dtracks = store.get_discovered_tracks()
    assert {d["video_id"] for d in dtracks} == {"vid1", "vid2"}
    assert all(d["genre"] == "Techno" for d in dtracks)
    # they were encoded into the shared content space (techno token exists in the model)
    dk, DV, _ = embed.load_discovered_content_vectors(store)
    assert DV is not None and len(dk) == 2


def test_populate_skips_owned_tracks(store):
    store.set_track_genre(store.upsert_track("vL", "LibT", "LibArtist", None, None), "Techno")
    # an owned track that the album also contains, must not be re-added as out-of-corpus
    from yt_playlist.util.matching import identity_key
    store.upsert_track("vOwned", "Track One", "New Artist", None, None)
    embed.build_content_and_store(store)
    store.upsert_discovered_album("alb1", "New Artist", "Some Album", "2024", None, 1.0, genre="Techno")
    discover.populate_discovered_tracks(_Ctx(store, _FakeClient()), now=1.0)
    keys = {d["identity_key"] for d in store.get_discovered_tracks()}
    assert identity_key("Track One", "New Artist") not in keys      # owned one skipped
    assert identity_key("Track Two", "New Artist") in keys
