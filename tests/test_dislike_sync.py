# tests/test_dislike_sync.py
from yt_playlist.library import sync
from yt_playlist.util.matching import identity_key
from tests.conftest import FakeClient


def _disliked(genre="metal"):
    return {"videoId": "v1", "title": "Bad Song", "artists": [{"name": "Nickelback"}],
            "album": {"name": "X"}, "duration_seconds": 200, "likeStatus": "DISLIKE"}


def test_sync_dislike_suppresses_and_feeds_negative_transient(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient(playlists=[{"playlistId": "PL1", "title": "Mix", "count": 1}],
                      tracks={"PL1": [_disliked()]})
    sync.sync_identity(store, iid, fake, now=1000.0)
    k = identity_key("Bad Song", "Nickelback")
    assert k in store.disliked_identity_keys()
    assert k in store.suppressed_keys("for_you", 1000.0)        # hidden everywhere
    # NO direct permanent axis nudge at capture (graduation owns that)
    assert all(v == 1.0 for v in store.get_weights().values())
    # #84: a dislike is a verdict on the TRACK, so the artist axis never accrues negatively (the
    # old assertion here encoded the leak #84 fixed). This fixture track has no genre at sync time
    # (enrichment hasn't run), so nothing accrues at all here; genre-facet accrual on dislikes is
    # covered by test_negative_graduation.py with enriched fixtures.
    assert store.get_theme("artist:Nickelback") is None


def test_sync_dislike_idempotent(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient(playlists=[{"playlistId": "PL1", "title": "Mix", "count": 1}],
                      tracks={"PL1": [_disliked()]})
    sync.sync_identity(store, iid, fake, now=1000.0)
    sync.sync_identity(store, iid, fake, now=2000.0)
    # #84: the artist axis no longer accrues on dislikes, and it must STAY empty on a repeat sync
    # (guards against a regression re-adding artist accrual on re-capture; genre-facet accrual and
    # idempotency with enriched fixtures live in test_negative_graduation.py).
    assert store.get_theme("artist:Nickelback") is None


def test_sync_reconciles_undislike(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = _disliked()
    fake = FakeClient(playlists=[{"playlistId": "PL1", "title": "Mix", "count": 1}], tracks={"PL1": [t]})
    sync.sync_identity(store, iid, fake, now=1000.0)
    t["likeStatus"] = "INDIFFERENT"
    sync.sync_identity(store, iid, fake, now=2000.0)
    k = identity_key("Bad Song", "Nickelback")
    assert k not in store.disliked_identity_keys()
    assert k not in store.suppressed_keys("for_you", 2000.0)
