"""Generated proto-playlists: the two dated, saveable lanes on Home.

Covers the load-bearing constraint — a playlist this app generates (auto-grouped "Generated") must
NOT feed the recommendation engine until it's played or re-grouped — plus the create endpoint.
"""
import json

from fastapi.testclient import TestClient

from yt_playlist.matching import identity_key
from yt_playlist.rec_dao import RecDao
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _seed_generated(store, iid, n=3):
    """A generated-group playlist of n tracks (unplayed). Returns (pid, ytm, track identity_keys)."""
    pid = store.upsert_playlist(iid, "PLG", "Gen - June 21 2026", n, "h", 1.0)
    tids = [store.upsert_track(f"g{i}", f"G{i}", "GenArt", None, None, 1) for i in range(n)]
    store.set_playlist_tracks(pid, tids)
    store.set_playlist_group("PLG", "Generated")
    return pid, "PLG", {identity_key(f"G{i}", "GenArt") for i in range(n)}


def test_generated_excluded_until_promoted(store):
    iid = store.upsert_identity("main", "cred", None, True)
    pid, ytm, gkeys = _seed_generated(store, iid, n=2)
    dao = RecDao(store)

    assert dao.excluded_playlist_ids() == {pid}                 # quarantined while "Generated"

    # Playing it does NOT graduate it — adoption is an explicit act, not a side effect of listening.
    for _ in range(5):
        store.add_history_snapshot(iid, 1.0, list(gkeys))
    assert dao.excluded_playlist_ids() == {pid}                 # still quarantined despite heavy plays

    store.set_playlist_group(ytm, "Faves")                      # promotion out of the group = adoption
    assert dao.excluded_playlist_ids() == set()

    store.set_playlist_group(ytm, "Generated")                  # back into quarantine
    assert dao.excluded_playlist_ids() == {pid}


def test_generated_tracks_in_no_basket_until_promoted(store):
    iid = store.upsert_identity("main", "cred", None, True)
    _pid, ytm, gkeys = _seed_generated(store, iid, n=3)
    dao = RecDao(store)

    for _ in range(2):
        store.add_history_snapshot(iid, 1.0, list(gkeys))      # even played, while still "Generated"...
    keys = {k for b in dao.rec_baskets() for k in b}
    assert not (keys & gkeys)                                   # ...generated songs pollute no basket

    store.set_playlist_group(ytm, "Faves")                     # promote it into the collection
    keys2 = {k for b in dao.rec_baskets() for k in b}
    assert gkeys & keys2                                        # ...so they rejoin the signal


def test_generate_endpoint_creates_and_groups(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})
    tracks = json.dumps([{"video_id": "v1", "title": "S1", "artist": "A", "album": "", "thumbnail": ""},
                         {"video_id": "v2", "title": "S2", "artist": "A", "album": "", "thumbnail": ""}])

    r = c.post("/home/generate", data={"name": "More in your wheelhouse - June 21 2026", "tracks": tracks})

    assert r.status_code == 200 and "Saved" in r.text
    assert fc.created and fc.created[0][1] == "More in your wheelhouse - June 21 2026"
    assert fc.added and fc.added[0][1] == ["v1", "v2"]
    new_ytm = fc.created[0][0]
    assert store.get_playlist_groups().get(new_ytm) == "Generated"   # auto-grouped
    # optimistically materialized so it shows in the Playlists tab right away (no sync needed)
    saved = next(p for p in store.get_playlists() if p.ytm_playlist_id == new_ytm)
    assert saved.title == "More in your wheelhouse - June 21 2026" and saved.track_count == 2


def test_saved_proto_tracks_not_re_offered(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = [store.upsert_track(f"v{i}", f"S{i}", "Art", None, None, 1) for i in range(2)]
    gen = store.upsert_playlist(iid, "PLG", "From your catalog - June 21 2026", 2, "h", 1.0)
    store.set_playlist_tracks(gen, t)
    store.set_playlist_group("PLG", "Generated")

    keys = RecDao(store).generated_track_keys()
    assert keys == {identity_key("S0", "Art"), identity_key("S1", "Art")}   # spoken for; don't re-offer


def test_generate_endpoint_rejects_empty(store):
    iid = store.upsert_identity("main", "cred", None, True)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/home/generate", data={"name": "x", "tracks": "[]"})
    assert r.status_code == 200 and "Couldn't save" in r.text


def test_home_renders_generated_cards(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Artist", "Alb", None, 1)
    pl = store.upsert_playlist(iid, "PL", "P", 1, "h", 1.0)
    store.set_playlist_tracks(pl, [t])
    store.add_history_snapshot(iid, 1.0, [identity_key("Song", "Artist")])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.get("/")
    assert r.status_code == 200
    assert "More in your wheelhouse" in r.text and "Save &amp; play on YouTube" in r.text


def test_create_generated_playlist_stores_recipe_and_versions(store):
    from yt_playlist import executor
    iid = store.upsert_identity("main", "cred", None, True)
    fc = FakeClient()
    tracks = [{"video_id": f"v{i}", "title": f"S{i}", "artist": "A" if i % 2 else "B"} for i in range(4)]
    recipe = {"model": "fresh", "facets": {"genres": ["house"], "eras": ["2010"]},
              "dj": {"stickiness": 0.0, "seed": 5}}
    r1 = executor.create_generated_playlist(store, "Fresh songs - June 21 2026", list(tracks),
                                            fc, now=1.0, identity_id=iid, recipe=recipe)
    assert r1["title"] == "Fresh songs - June 21 2026 #1"          # versioned at save
    assert store.get_recipe(r1["new_ytm"])["facets"]["genres"] == ["house"]   # recipe stored
    r2 = executor.create_generated_playlist(store, "Fresh songs - June 21 2026", list(tracks),
                                            fc, now=2.0, identity_id=iid, recipe=recipe)
    assert r2["title"] == "Fresh songs - June 21 2026 #2"          # next version that day
