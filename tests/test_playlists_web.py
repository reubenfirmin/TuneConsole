"""Contract tests for the htmx Playlists bulk actions (/playlists/copy|group|delete).

The bulk routes now do their store/YouTube work and return an empty 200 with
HX-Refresh: true (htmx then does a full page reload — parity with the old
location.reload()), instead of the old JSON payloads. Fast TestClient assertions
on the header + the store mutation.

Store-mutation coverage moved here from the JSON-based test_web.py tests
(test_playlists_group_and_delete / _copy_and_copy_merge / _delete_hides_system).
"""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


def _client(store, provider):
    # local base_url so state-changing POSTs pass the cross-origin guard.
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _refreshes(r):
    return r.status_code == 200 and r.headers.get("hx-refresh") == "true"


def test_group_assigns_and_refreshes(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Alpha", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Beta", 1, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/group", data={"ids": f"{a},{b}", "name": "Faves"})
    assert _refreshes(r)
    assert r.text == ""                                # no JSON body — htmx just reloads
    assert store.get_playlist_groups() == {"PLA": "Faves", "PLB": "Faves"}


def test_delete_removes_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Alpha", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v1", "S", "X", None, None, 1)])
    fc = FakeClient(tracks={"PLA": [_track("v1", "S", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/delete", data={"ids": str(a)})
    assert _refreshes(r)
    assert store.get_playlist(a) is None and fc.deleted == ["PLA"]


def test_delete_hides_system_playlist_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)   # undeletable system playlist
    store.set_playlist_tracks(lm, [store.upsert_track("v1", "S", "X", None, None, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/delete", data={"ids": str(lm)})
    assert _refreshes(r)
    assert store.get_playlist(lm) is not None          # survives on YouTube
    assert "LM" in store.get_hidden_playlists()        # just hidden from the tab


def test_copy_creates_playlist_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Rock", 2, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S0", "X", None, None, 1),
                                  store.upsert_track("v1", "S1", "X", None, None, 1)])
    fc = FakeClient(tracks={"PLA": [_track("v0", "S0", "X"), _track("v1", "S1", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/copy", data={"ids": str(a), "name": "Rock Copy"})
    assert _refreshes(r)
    assert any(p.title == "Rock Copy" for p in store.get_playlists())   # pulled into the store


def test_copy_merge_unions_tracks_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Rock", 2, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Pop", 2, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(3)]
    store.set_playlist_tracks(a, [t[0], t[1]]); store.set_playlist_tracks(b, [t[1], t[2]])
    fc = FakeClient(tracks={"PLA": [_track("v0", "S0", "X"), _track("v1", "S1", "X")],
                            "PLB": [_track("v1", "S1", "X"), _track("v2", "S2", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/copy", data={"ids": f"{a},{b}", "name": "Combined"})
    assert _refreshes(r)
    combined = next(p for p in store.get_playlists() if p.title == "Combined")
    assert combined.track_count == 3                   # union of v0,v1,v2


def test_copy_into_appends_union_skipping_dupes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    src = store.upsert_playlist(iid, "PLA", "Rock", 2, "h", 1.0)
    dst = store.upsert_playlist(iid, "PLB", "Dest", 1, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(3)]
    store.set_playlist_tracks(src, [t[0], t[1], t[2]])
    store.set_playlist_tracks(dst, [t[1]])             # v1 already in the destination
    fc = FakeClient(tracks={"PLA": [_track("v0", "S0", "X"), _track("v1", "S1", "X"), _track("v2", "S2", "X")],
                            "PLB": [_track("v1", "S1", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/copy-into", data={"ids": str(src), "target": str(dst)})
    assert _refreshes(r)
    assert store.get_playlist_track_ids(dst) == [t[1], t[0], t[2]]   # existing kept, v0/v2 appended
    assert fc.added == [("PLB", ["v0", "v2"])]                       # v1 skipped (already present)


def test_copy_into_requires_a_destination(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Rock", 1, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/copy-into", data={"ids": str(a), "target": ""})
    assert r.status_code == 422 and "destination" in r.text.lower()  # toast, not a refresh


def test_copy_into_rejects_system_target(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    src = store.upsert_playlist(iid, "PLA", "Rock", 1, "h", 1.0)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 1.0)   # system playlist
    store.set_playlist_tracks(src, [store.upsert_track("v0", "S0", "X", None, None, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/copy-into", data={"ids": str(src), "target": str(lm)})
    assert r.status_code == 422 and "system playlist" in r.text.lower()


def test_copy_into_rejects_cross_account_target(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    a = store.upsert_identity("main", "cred", None, True)
    b = store.upsert_identity("alt", "cred2", "BA", False)             # a second account
    src = store.upsert_playlist(a, "PLA", "Rock", 1, "h", 1.0)         # source on account A
    dst = store.upsert_playlist(b, "PLB", "Dest", 0, "h", 1.0)         # target on account B
    store.set_playlist_tracks(src, [store.upsert_track("v0", "S0", "X", None, None, 1)])
    c = _client(store, lambda: {a: FakeClient(), b: FakeClient()})

    r = c.post("/playlists/copy-into", data={"ids": str(src), "target": str(dst)})
    assert r.status_code == 422 and "same account" in r.text.lower()    # cross-account add refused


def test_promote_moves_playlist_out_of_generated(store):
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PLG", "Gen Mix", 3, "h", 0.0)
    store.set_playlist_group("PLG", "Generated")
    assert store.get_playlist_groups().get("PLG") == "Generated"
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{pid}/promote")
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    assert store.get_playlist_groups().get("PLG", "") != "Generated"   # graduated out of quarantine


def test_playlists_page_carries_generated_created_at(store):
    # The Generated card is ordered newest-first client-side (genRows in app.js sorts by `created`),
    # which relies on each generated playlist's recipe created_at flowing into the page rows.
    import json
    import re
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLold", "Older Gen", 1, "h", 0.0)
    store.upsert_playlist(iid, "PLnew", "Newer Gen", 1, "h", 0.0)
    for ytm in ("PLold", "PLnew"):
        store.set_playlist_group(ytm, "Generated")
    store.set_recipe("PLold", {"theme": "a"}, 100.0)
    store.set_recipe("PLnew", {"theme": "b"}, 200.0)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.get("/playlists")
    assert r.status_code == 200
    rows = json.loads(re.search(r"playlistsTab\((\[.*?\])\)", r.text).group(1))
    created = {row["ytm"]: row["created"] for row in rows}
    assert created["PLold"] == 100.0 and created["PLnew"] == 200.0


def test_waterfall_registry_includes_all_providers():
    from yt_playlist.providers import waterfall
    # the waterfall harness can dispatch to every provider, each exposing the probe interface
    assert set(waterfall.REGISTRY) == {"musicbrainz", "lastfm", "discogs", "deezer", "acousticbrainz"}
    for mod in waterfall.REGISTRY.values():
        assert hasattr(mod, "probe") and hasattr(mod, "available")
