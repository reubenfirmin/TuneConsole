# tests/test_dislike_sync.py
from yt_playlist import sync
from yt_playlist.matching import identity_key
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
    # dislike accumulated negatively into the graduation ledger
    assert (store.get_theme("artist:Nickelback") or 0.0) < 0


def test_sync_dislike_idempotent(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient(playlists=[{"playlistId": "PL1", "title": "Mix", "count": 1}],
                      tracks={"PL1": [_disliked()]})
    sync.sync_identity(store, iid, fake, now=1000.0)
    sync.sync_identity(store, iid, fake, now=2000.0)
    assert (store.get_theme("artist:Nickelback") or 0.0) > -1.5 - 1e-9   # accumulated once, not twice
    # (one dislike -> one -1.0 contribution; a second sync must not add another)
    assert store.get_theme("artist:Nickelback") == -1.0


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
