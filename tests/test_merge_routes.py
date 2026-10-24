"""Merge editor selections, ordering and execution routes."""
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.conftest import FakeClient, _track
from tests.web_support import _client
@pytest.mark.parametrize("return_to, label", [
    ("/cleanup", "Back to cleanup"),
    ("/cleanup#overlaps", "Back to cleanup"),
    ("/playlists?sort=name#list", "Back to playlists"),
])
def test_merge_apply_route(store, monkeypatch, tmp_path, return_to, label):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "A", 1, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "B", 1, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "One", "X", None, None, 1)
    t2 = store.tracks.upsert_track("v2", "Two", "X", None, None, 1)
    store.playlists.set_playlist_tracks(a, [t1]); store.playlists.set_playlist_tracks(b, [t2])
    client = FakeClient(tracks={"PLA": [_track("v1", "One", "X")],
                                "PLB": [_track("v2", "Two", "X")]})
    c = _client(store, lambda: {iid: client})
    page = c.get("/merge", params={"ids": f"{a},{b}", "return": return_to})
    assert label in page.text and 'class="ui-arrow ui-arrow--left"' in page.text
    assert f'href="{return_to}"' in page.text
    r = c.post(f"/merge/apply?ids={a},{b}")
    assert r.status_code == 200
    destination = urlsplit(r.headers["hx-redirect"])
    expected = urlsplit(return_to)
    assert destination.path == expected.path
    assert destination.fragment == expected.fragment
    query = parse_qs(destination.query)
    assert "Merged into" in query["flash"][0]
    assert query["flash_pl"] == ["PLA"]
    if expected.query:
        assert query["sort"] == ["name"]
    assert client.deleted == ["PLB"] and store.playlists.get_playlist(b) is None
    log = c.get("/actions").text
    assert "merged into “A”, deleted “B”" in log
    assert "merge_draft" not in log
    assert "/undo/" in log


def test_merge_apply_ignores_stale_ids_in_url(store, monkeypatch, tmp_path):
    # a non-existent id in ?ids (e.g. a playlist deleted after the link was made) must not break Apply:
    # the editor renders the existing playlists, and Apply operates on that filtered member set.
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "A", 1, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "B", 1, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "One", "X", None, None, 1)
    store.playlists.set_playlist_tracks(a, [t1]); store.playlists.set_playlist_tracks(b, [t1])
    client = FakeClient(tracks={pid: [_track("v1", "One", "X")] for pid in ("PLA", "PLB")})
    c = _client(store, lambda: {iid: client})
    c.get(f"/merge?ids={a},{b},99999")                    # 99999 doesn't exist
    r = c.post(f"/merge/apply?ids={a},{b},99999")
    assert r.status_code == 200 and r.headers.get("hx-redirect")   # applied (not the 422 error toast)
    assert store.playlists.get_playlist(b) is None                  # kept A, deleted B -> merge ran

def test_merge_editor_renders_nway(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "Mix", 2, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Mix", 2, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "One", "X", None, 1); t2 = store.tracks.upsert_track("v2", "Two", "X", None, 1)
    store.playlists.set_playlist_tracks(a, [t1, t2]); store.playlists.set_playlist_tracks(b, [t1])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/merge?ids={a},{b}").text
    assert "Where should the result go" in body and "One" in body and "Two" in body
    # /dupe redirects into the same editor
    assert c.get(f"/dupe/{a}/{b}").status_code == 200


def test_merge_update_toggle_and_setall_persist(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "Mix", 2, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Mix2", 2, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "One", "X", None, None, 1)
    t2 = store.tracks.upsert_track("v2", "Two", "X", None, None, 1)
    store.playlists.set_playlist_tracks(a, [t1, t2]); store.playlists.set_playlist_tracks(b, [t1])
    c = _client(store, lambda: {iid: FakeClient()})
    c.get(f"/merge?ids={a},{b}")                          # all included -> count 2 / 2
    upd = f"/merge/update?ids={a},{b}"
    r = c.post(upd, data={"field": "toggle", "value": "v:v1"})
    assert r.status_code == 200 and "1 / 2" in r.text     # one excluded now
    log = c.get("/actions").text
    assert "merge_draft" not in log and "No actions yet." in log
    assert store.actions.get_draft(f"{a},{b}") is not None
    # the draft survives a "refresh" (re-GET), v1 still excluded
    assert "1 / 2" in c.get(f"/merge?ids={a},{b}").text
    assert "0 / 2" in c.post(upd, data={"field": "setall", "value": "0"}).text   # none
    assert "2 / 2" in c.post(upd, data={"field": "setall", "value": "1"}).text   # all back


def test_merge_update_sort_mode_keep_pick(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "Mix", 2, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Mix2", 1, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "Beta", "X", None, None, 1)
    t2 = store.tracks.upsert_track("v2", "Alpha", "X", None, None, 1)
    store.playlists.set_playlist_tracks(a, [t1, t2]); store.playlists.set_playlist_tracks(b, [t1])
    c = _client(store, lambda: {iid: FakeClient()})
    c.get(f"/merge?ids={a},{b}")
    upd = f"/merge/update?ids={a},{b}"
    alpha = c.post(upd, data={"field": "sort", "value": "alpha"}).text
    assert alpha.index("Alpha") < alpha.index("Beta")       # alpha sort orders by title
    # mode / keep / pick / an unknown field all dispatch and re-render without error
    for field, value in (("mode", "ducks"), ("keep", "all"), ("pick", "v:v1:1"), ("bogus", "x")):
        assert c.post(upd, data={"field": field, "value": value}).status_code == 200
    assert 'value="all" checked' in c.get(f"/merge?ids={a},{b}").text   # keep choice survived the refresh


