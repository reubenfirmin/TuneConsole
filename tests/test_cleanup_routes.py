"""Duplicate and overlap cleanup, deletion guards and undo routes."""
import json

import pytest
from fastapi.testclient import TestClient
from yt_playlist.rec import actions
from yt_playlist.rec.actions import CLEANUP_SURFACE
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track
from tests.web_support import _client
def test_dupe_detail_renders(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "A", 1, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "B", 1, "h", 1.0)
    t = store.tracks.upsert_track("v1", "Shared", "X", None, None)
    store.playlists.set_playlist_tracks(a, [t]); store.playlists.set_playlist_tracks(b, [t])
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    r = c.get(f"/dupe/{a}/{b}")
    assert r.status_code == 200
    assert "shared|x" in r.text.lower() or "shared" in r.text.lower()

def test_dupe_detail_unknown_id_404(store):
    store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app)
    assert c.get("/dupe/999/998").status_code == 404

def test_identical_pair_opens_editor(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "Mix A", 1, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Mix B", 1, "h", 1.0)
    t = store.tracks.upsert_track("v1", "Song", "X", None, None)
    store.playlists.set_playlist_tracks(a, [t]); store.playlists.set_playlist_tracks(b, [t])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/dupe/{a}/{b}").text   # redirects into the N-way editor
    assert "Where should the result go" in body and "Keep" in body


def test_overlap_suppress_and_unsuppress(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fav = store.playlists.upsert_playlist(iid, "PLFAV", "Favorite Songs", 2, "h", 1.0)
    sml = store.playlists.upsert_playlist(iid, "PLSML", "Little Mix", 1, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "A", "X", None, 1); t2 = store.tracks.upsert_track("v2", "B", "X", None, 1)
    store.playlists.set_playlist_tracks(fav, [t1, t2]); store.playlists.set_playlist_tracks(sml, [t1])
    c = _client(store, lambda: {iid: FakeClient()})
    assert "Little Mix" in c.get("/cleanup").text
    r = c.post("/overlaps/suppress", data={"a": "PLFAV", "b": "PLSML"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"   # pair -> Hidden, recompute
    # check the section header specifically (other copy may mention "hidden")
    assert 'Hidden overlaps <span class="count">' in c.get("/cleanup").text
    c.post("/overlaps/unsuppress", data={"a": "PLSML", "b": "PLFAV"})
    assert 'Hidden overlaps <span class="count">' not in c.get("/cleanup").text


def test_overlaps_suppress_many(store):
    import json
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fav = store.playlists.upsert_playlist(iid, "PLFAV", "Favorite Songs", 3, "h", 1.0)
    p1 = store.playlists.upsert_playlist(iid, "PL1", "One", 2, "h", 1.0)
    p2 = store.playlists.upsert_playlist(iid, "PL2", "Two", 1, "h", 1.0)
    t = [store.tracks.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(3)]
    store.playlists.set_playlist_tracks(fav, t)
    store.playlists.set_playlist_tracks(p1, [t[0], t[1]])
    store.playlists.set_playlist_tracks(p2, [t[0]])
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/overlaps/suppress-many",
               data={"pairs": json.dumps([["PLFAV", "PL2"], ["PLFAV", "PL1"]])})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    assert "Hidden overlaps" in c.get("/cleanup").text
    # malformed entries are ignored, not fatal
    r = c.post("/overlaps/suppress-many", data={"pairs": json.dumps([["only-one"], "junk", []])})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"


@pytest.mark.parametrize('pairs', ['null', '{}', '42', '"junk"', 'broken',
                                  '[[1,"PLB"],[{},"PLB"],["PLA",null]]'])
def test_overlaps_suppress_many_ignores_malformed_input(store, pairs):
    response = _client(store, lambda: {}).post('/overlaps/suppress-many', data={'pairs': pairs})
    assert response.status_code == 200 and response.headers['HX-Refresh'] == 'true'
    assert store.overlaps.get_suppressed_overlaps() == []


def test_inline_dupe_delete_ok(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "X", None, 1)
    keep = store.playlists.upsert_playlist(iid, "KEEP", "Storm", 1, "h", 1.0); store.playlists.set_playlist_tracks(keep, [t])
    dele = store.playlists.upsert_playlist(iid, "DEL", "Storm", 1, "h", 1.0); store.playlists.set_playlist_tracks(dele, [t])
    client = FakeClient(tracks={"KEEP": [_track("v1", "Song", "X")], "DEL": [_track("v1", "Song", "X")]})
    c = _client(store, lambda: {iid: client})
    r = c.post("/dupe/delete", data={"source": dele, "target": keep})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.deleted == ["DEL"] and store.playlists.get_playlist(dele) is None

def test_inline_dupe_delete_refuses_survivor(store, monkeypatch, tmp_path):
    # Foot-gun guard: after one copy is gone, deleting the other must be refused (kept copy missing).
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "X", None, 1)
    survivor = store.playlists.upsert_playlist(iid, "SURV", "Storm", 1, "h", 1.0); store.playlists.set_playlist_tracks(survivor, [t])
    gone = store.playlists.upsert_playlist(iid, "GONE", "Storm", 1, "h", 1.0); store.playlists.set_playlist_tracks(gone, [t])
    # remote: SURV has the track, GONE no longer exists (deleted) -> cannot serve as the kept copy
    client = FakeClient(tracks={"SURV": [_track("v1", "Song", "X")]})
    c = _client(store, lambda: {iid: client})
    r = c.post("/dupe/delete", data={"source": survivor, "target": gone})  # delete survivor, "keep" the gone one
    assert r.status_code == 200 and r.json()["ok"] is False
    assert client.deleted == [] and store.playlists.get_playlist(survivor) is not None  # survivor untouched


def test_dupe_keep_one_deletes_whole_cluster(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "X", None, 1)
    # three identical copies
    ids = []
    for ytm in ("PLa", "PLb", "PLc"):
        p = store.playlists.upsert_playlist(iid, ytm, "Afternoon mix", 1, "h", 1.0); store.playlists.set_playlist_tracks(p, [t])
        ids.append((p, ytm))
    keep_id, _ = ids[0]
    client = FakeClient(tracks={ytm: [_track("v1", "Song", "X")] for _, ytm in ids})
    c = _client(store, lambda: {iid: client})
    r = c.post("/dupe/keep-one", data={"keep": keep_id})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"   # success -> recompute page
    remaining = {p.ytm_playlist_id for p in store.playlists.get_playlists()}
    assert remaining == {"PLa"}                      # only the kept copy survives
    assert sorted(client.deleted) == ["PLb", "PLc"]


def test_delete_empty_playlist(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    empty = store.playlists.upsert_playlist(iid, "PLempty", "jazz 2", 0, "h", 1.0)      # no tracks set
    client = FakeClient()                                                     # get_playlist -> {tracks: []}
    c = _client(store, lambda: {iid: client})
    r = c.post("/playlist/delete-empty", data={"playlist": empty})
    assert r.status_code == 200 and r.text.strip() == ""          # empty -> htmx removes the row
    assert client.deleted == ["PLempty"] and store.playlists.get_playlist(empty) is None

def test_inline_dupe_delete_refreshes_cached_cleanup_summary(store, monkeypatch, tmp_path):
    # /dupe/delete returns JSON without a page reload, so it must refresh the cached cleanup
    # summary itself or the home card keeps showing the deleted playlist as pending (issue #73).
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = [store.tracks.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(4)]
    tr = [_track(f"v{i}", f"S{i}", "X") for i in range(4)]
    keep = store.playlists.upsert_playlist(iid, "KEEP", "Storm", 4, "h", 1.0); store.playlists.set_playlist_tracks(keep, t)
    dele = store.playlists.upsert_playlist(iid, "DEL", "Storm", 4, "h", 1.0); store.playlists.set_playlist_tracks(dele, t)
    actions.refresh_cleanup(store, 1.0)
    assert store.rec_proposals.get_proposals(CLEANUP_SURFACE)["count"] == 2
    client = FakeClient(tracks={"KEEP": tr, "DEL": tr})
    c = _client(store, lambda: {iid: client})
    r = c.post("/dupe/delete", data={"source": dele, "target": keep})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.rec_proposals.get_proposals(CLEANUP_SURFACE)["count"] == 0      # survivor is no longer a dupe


def test_delete_empty_refreshes_cached_cleanup_summary(store, monkeypatch, tmp_path):
    # /playlist/delete-empty just removes the row (no reload), so it must refresh the cache too.
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    empty = store.playlists.upsert_playlist(iid, "PLempty", "jazz 2", 0, "h", 1.0)
    actions.refresh_cleanup(store, 1.0)
    assert store.rec_proposals.get_proposals(CLEANUP_SURFACE)["count"] == 1      # the empty playlist
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/playlist/delete-empty", data={"playlist": empty})
    assert r.status_code == 200
    assert store.rec_proposals.get_proposals(CLEANUP_SURFACE)["count"] == 0


def test_delete_empty_refuses_if_not_empty(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pl = store.playlists.upsert_playlist(iid, "PLfull", "actually full", 1, "h", 1.0)
    client = FakeClient(tracks={"PLfull": [_track("v1", "S", "X")]})          # has a track remotely
    c = _client(store, lambda: {iid: client})
    r = c.post("/playlist/delete-empty", data={"playlist": pl})
    assert r.status_code == 422 and client.deleted == []          # refused -> error toast, nothing deleted


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), KeyError("musicPlaylistShelfRenderer")])
def test_delete_empty_failed_read_keeps_row_and_cleanup_summary(store, monkeypatch, tmp_path, failure):
    from yt_playlist.library import executor
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    monkeypatch.setattr(executor, "with_retry", lambda fn: fn())
    iid = store.identities.upsert_identity("main", "cred", None, True)
    empty = store.playlists.upsert_playlist(iid, "PLempty", "Empty at last sync", 0, "h", 1.0)
    client = FakeClient(tracks={"PLempty": [_track("new", "Added elsewhere", "X")]})

    def unreadable(playlist, limit=None):
        raise failure

    monkeypatch.setattr(client, "get_playlist", unreadable)
    actions.refresh_cleanup(store, 1.0)
    summary = store.rec_proposals.get_proposals(CLEANUP_SURFACE)
    c = _client(store, lambda: {iid: client})
    r = c.post("/playlist/delete-empty", data={"playlist": empty})
    assert r.status_code == 422 and r.headers["hx-reswap"] == "none"
    assert "empty on YouTube" in r.text and "Nothing was changed" in r.text
    assert client.deleted == [] and store.playlists.get_playlist(empty) is not None
    assert store.rec_proposals.get_proposals(CLEANUP_SURFACE) == summary
    assert store.actions.get_actions() == []
    assert not list(tmp_path.glob("backups/*"))


def test_overlap_ignore_excludes_playlist(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fav = store.playlists.upsert_playlist(iid, "PLFAV", "Big Mixtape", 3, "h", 1.0)
    a = store.playlists.upsert_playlist(iid, "PLa", "A", 1, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLb", "B", 1, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "S1", "X", None, 1); t2 = store.tracks.upsert_track("v2", "S2", "X", None, 1)
    store.playlists.set_playlist_tracks(fav, [t1, t2]); store.playlists.set_playlist_tracks(a, [t1]); store.playlists.set_playlist_tracks(b, [t2])
    c = _client(store, lambda: {iid: FakeClient()})
    assert "Big Mixtape" in c.get("/cleanup").text                       # overlaps present
    r = c.post("/overlaps/ignore", data={"ytm": "PLFAV"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    body = c.get("/cleanup").text
    # overlaps card spans from its header to the next section header
    overlaps_card = body.split('id="overlaps"')[1].split('<h2 class="section')[0]
    assert "Big Mixtape" not in overlaps_card                     # gone from overlaps table
    assert "Playlists excluded from overlap checks" in body
    c.post("/overlaps/unignore", data={"ytm": "PLFAV"}, follow_redirects=False)
    assert "Big Mixtape" in c.get("/cleanup").text                       # back

def test_system_playlist_excluded_from_overlaps(store):
    from yt_playlist.library.analysis import find_overlaps
    iid = store.identities.upsert_identity("main", "cred", None, True)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 2, "h", 1.0)   # system
    a = store.playlists.upsert_playlist(iid, "PLa", "A", 1, "h", 1.0)
    t1 = store.tracks.upsert_track("v1", "S1", "X", None, 1); t2 = store.tracks.upsert_track("v2", "S2", "X", None, 1)
    store.playlists.set_playlist_tracks(lm, [t1, t2]); store.playlists.set_playlist_tracks(a, [t1])
    assert find_overlaps(store) == []   # Liked Music never generates overlaps


def test_undo_via_dupe_delete(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "X", None, 1)
    keep = store.playlists.upsert_playlist(iid, "KEEP", "Mix", 1, "h", 1.0); store.playlists.set_playlist_tracks(keep, [t])
    dele = store.playlists.upsert_playlist(iid, "DEL", "Mix", 1, "h", 1.0); store.playlists.set_playlist_tracks(dele, [t])
    client = FakeClient(tracks={"KEEP": [_track("v1", "Song", "X")], "DEL": [_track("v1", "Song", "X")]},
                        catalog={"v1": _track("v1", "Song", "X")})
    c = _client(store, lambda: {iid: client})
    assert c.post("/dupe/delete", data={"source": dele, "target": keep}).json()["ok"] is True
    aid = store.actions.get_actions()[0].id
    assert "/undo/" in c.get("/actions").text                       # undo offered for the executed delete
    r = c.post(f"/undo/{aid}", follow_redirects=False)
    assert r.status_code == 303
    assert store.actions.get_action(aid).status == "undone" and client.created  # DEL recreated from backup


def test_overlap_keep_only_mutes_others(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fav = store.playlists.upsert_playlist(iid, "PLFAV", "Favorite Songs", 3, "h", 1.0)
    other = store.playlists.upsert_playlist(iid, "PLB", "Favorite Songs 2", 2, "h", 1.0)
    noise = store.playlists.upsert_playlist(iid, "PLn", "Little Mix", 1, "h", 1.0)
    t = [store.tracks.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(3)]
    store.playlists.set_playlist_tracks(fav, [t[0], t[1], t[2]]); store.playlists.set_playlist_tracks(other, [t[0], t[1]])
    store.playlists.set_playlist_tracks(noise, [t[2]])   # shares only with PLFAV (t2), not PLB
    c = _client(store, lambda: {iid: FakeClient()})
    # mute PLFAV's other overlaps but keep the PLFAV–PLB pair
    r = c.post("/overlaps/mute-others", data={"a": "PLFAV", "b": "PLB"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    # scope to the overlaps card only (a 1-track playlist also shows under "Tiny playlists")
    overlaps_card = c.get("/cleanup").text.split('id="overlaps"')[1].split('<h2 class="section')[0]
    assert "Favorite Songs 2" in overlaps_card     # kept pair still shown
    assert "Little Mix" not in overlaps_card       # the noise overlap is muted


def test_delete_empty_removes_row(store,monkeypatch,tmp_path):
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PLE", "Empty One", 0, "h", 1.0)   # no tracks
    c = _client(store, lambda: {iid: FakeClient()})
    assert c.get("/cleanup").text.count('class="empty-row"') == 1
    r = c.post("/playlist/delete-empty", data={"playlist": pid})
    assert r.status_code == 200 and r.text.strip() == ""       # empty -> htmx removes the row
    assert store.playlists.get_playlist(pid) is None


def test_unhide_overlap_restores_to_overlaps_section(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "Alpha", 3, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Beta", 3, "h", 1.0)
    t = [store.tracks.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(4)]
    store.playlists.set_playlist_tracks(a, [t[0], t[1], t[2]])           # {0,1,2}
    store.playlists.set_playlist_tracks(b, [t[1], t[2], t[3]])           # {1,2,3}: shared {1,2}, jaccard .5 -> overlap
    c = _client(store, lambda: {iid: FakeClient()})

    store.overlaps.suppress_overlap("PLA", "PLB", 1.0)                  # hide the pair
    assert c.get("/cleanup").text.count('class="ov-row"') == 0  # gone from Overlaps, now Hidden

    r = c.post("/overlaps/unsuppress", data={"a": "PLA", "b": "PLB"})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"   # recompute, not drop-in-place
    assert c.get("/cleanup").text.count('class="ov-row"') == 1  # restored to the Overlaps section


def test_keep_one_refreshes_and_deletes_other_copies(store,monkeypatch,tmp_path):
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PLA", "Dup A", 2, "h", 1.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Dup B", 2, "h", 1.0)
    t = [store.tracks.upsert_track(f"d{i}", f"D{i}", "X", None, None, 1) for i in range(2)]
    store.playlists.set_playlist_tracks(a, t); store.playlists.set_playlist_tracks(b, t)   # identical -> a dup group
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post("/dupe/keep-one", data={"keep": a})
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    assert store.playlists.get_playlist(a) is not None and store.playlists.get_playlist(b) is None   # other copy deleted


