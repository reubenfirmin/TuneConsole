from fastapi.testclient import TestClient

from yt_playlist.rec import embed
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def test_playlist_local_dismiss_only_affects_that_playlist(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    anchor = store.tracks.upsert_track("v1", "Anchor", "Band", None, None)
    bonus = store.tracks.upsert_track("v2", "Bonus", "Band", None, None)
    store.tracks.set_track_genre(anchor, "Techno")
    store.tracks.set_track_genre(bonus, "Techno")
    target = store.playlists.upsert_playlist(iid, "PT", "Target", 1, "h", 0.0)
    store.playlists.set_playlist_tracks(target, [anchor])
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PO", "Other", 2, "h2", 0.0), [anchor, bonus])
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")

    assert "Bonus" in c.get(f"/playlist/{target}/suggestions").text
    # dismiss "doesn't fit here" scoped to this playlist
    r = c.post("/recs/feedback", data={"item": "bonus|band", "surface": "suggest",
                                       "scope": str(target), "kind": "dismiss"})
    assert r.status_code == 200
    # gone from THIS playlist's suggestions...
    assert "Bonus" not in c.get(f"/playlist/{target}/suggestions").text
    # ...but a different playlist's suggestions are unaffected (scope is local)
    assert "bonus|band" not in store.rec_model.suppressed_keys("suggest", now=2.0, scope="9999")
