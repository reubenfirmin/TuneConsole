"""Category-scoped cleanup ignores, end-to-end through the routes: a per-playlist dismissal (Empty/
Tiny) and a per-merge dismissal (Exact/Near duplicates) both drop the item from the cleanup page AND
the home 'Playlist cleanups' count, and unignoring restores it."""
import json

import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import actions
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _app(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                           base_url="http://127.0.0.1")


def _count_after_reload(c, store):
    """The home count after the browser's HX-Refresh reload of /cleanup (which re-materializes it)."""
    c.get("/cleanup")                                             # the reload every cleanup edit triggers
    return (store.rec_proposals.get_proposals("cleanup") or {}).get("count", 0)


def test_ignore_empty_playlist_round_trip(store):
    iid, c = _app(store)
    store.playlists.upsert_playlist(iid, "PE", "Strays", 0, "h", 0.0)        # one empty playlist
    assert _count_after_reload(c, store) == 1
    assert "Strays" in c.get("/cleanup").text

    c.post("/cleanup/ignore", data={"ytm": "PE", "category": "empty"})
    assert _count_after_reload(c, store) == 0                      # gone from the home count
    assert "Skipped cleanup suggestions" in c.get("/cleanup").text
    assert not any(a.kind == "cleanup" for a in actions.take_action(store, 1001.0, {}))

    c.post("/cleanup/unignore", data={"ytm": "PE", "category": "empty"})
    assert _count_after_reload(c, store) == 1                      # restored


def test_ignore_merge_round_trip(store):
    iid, c = _app(store)
    a = store.playlists.upsert_playlist(iid, "PLA", "Mix", 4, "h", 0.0)
    b = store.playlists.upsert_playlist(iid, "PLB", "Mix copy", 4, "h", 0.0)
    ts = [store.tracks.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(4)]   # 4 tracks -> not 'tiny'
    store.playlists.set_playlist_tracks(a, ts); store.playlists.set_playlist_tracks(b, ts)            # identical -> a merge
    assert _count_after_reload(c, store) == 2                      # both copies involved

    c.post("/cleanup/ignore-merge", data={"members": "PLA,PLB"})
    assert _count_after_reload(c, store) == 0                      # the merge (and its playlists) gone
    assert "Skipped cleanup suggestions" in c.get("/cleanup").text

    c.post("/cleanup/unignore-merge", data={"signature": "PLA|PLB"})
    assert _count_after_reload(c, store) == 2                      # restored


@pytest.mark.parametrize("near", [False, True])
def test_dismissed_duplicate_members_still_show_unrelated_overlaps(store, near):
    from yt_playlist.library.analysis import analyze_cleanup
    iid, c = _app(store)
    tracks = [store.tracks.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(10)]
    members = [tracks[:6], tracks[:5] + [tracks[6]], tracks[:2] + tracks[7:9]] if near else [
        tracks[:4], tracks[:4], tracks[:2] + tracks[7:9]]
    for remote, rows in zip(("PLA", "PLB", "PLC"), members):
        pid = store.playlists.upsert_playlist(iid, remote, remote, len(rows), "h", 0)
        store.playlists.set_playlist_tracks(pid, rows)
    c.post("/cleanup/ignore-merge", data={"members": "PLA,PLB"})
    result = analyze_cleanup(store)
    assert not result.groups and not result.near_pairs
    assert {frozenset((o.playlist_a.ytm_playlist_id, o.playlist_b.ytm_playlist_id))
            for o in result.overlaps} == {frozenset(("PLA", "PLC")), frozenset(("PLB", "PLC"))}
    assert _count_after_reload(c, store) == 3


def _near_playlists(store, iid):
    tracks = [store.tracks.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(10)]
    for n, ytm in enumerate(("PLA", "PLB", "PLC")):
        pid = store.playlists.upsert_playlist(iid, ytm, f"Similar {ytm}", 8, "h", 0.0)
        store.playlists.set_playlist_tracks(pid, tracks[:7] + [tracks[7 + n]])


@pytest.mark.parametrize("action,data,remaining_pairs,remaining_playlists", [
    ("suppress", {"a": "PLA", "b": "PLB"}, 2, 3),
    ("ignore", {"ytm": "PLA"}, 1, 2),
    ("ignore", {"ytm": "PLB"}, 1, 2),
    ("mute-others", {"a": "PLA", "b": "PLB"}, 1, 2),
    ("suppress-many", {"pairs": json.dumps([["PLA", "PLB"], ["PLA", "PLC"], ["PLB", "PLC"]])}, 0, 0),
])
def test_near_duplicate_menu_actions_update_page_and_home_count(
        store, action, data, remaining_pairs, remaining_playlists):
    iid, c = _app(store)
    _near_playlists(store, iid)
    assert c.get("/cleanup").text.count('class="ov-row"') == 3
    assert _count_after_reload(c, store) == 3

    response = c.post(f"/overlaps/{action}", data=data)
    assert response.headers["hx-refresh"] == "true"
    assert c.get("/cleanup").text.count('class="ov-row"') == remaining_pairs
    assert _count_after_reload(c, store) == remaining_playlists
    if action == "mute-others":
        assert 'data-ay="PLA" data-by="PLB"' in c.get("/cleanup").text


@pytest.mark.parametrize("hide,restore,data", [
    ("suppress", "unsuppress", {"a": "PLA", "b": "PLB"}),
    ("ignore", "unignore", {"ytm": "PLA"}),
])
def test_near_duplicate_preferences_restore_pairs(store, hide, restore, data):
    iid, c = _app(store)
    _near_playlists(store, iid)
    c.post(f"/overlaps/{hide}", data=data)
    c.post(f"/overlaps/{restore}", data=data)
    assert c.get("/cleanup").text.count('class="ov-row"') == 3
    assert _count_after_reload(c, store) == 3


def test_previously_ignored_near_merge_stays_hidden(store):
    iid, c = _app(store)
    _near_playlists(store, iid)
    c.post("/cleanup/ignore-merge", data={"members": "PLA,PLB,PLC"})
    assert c.get("/cleanup").text.count('class="ov-row"') == 0
    assert _count_after_reload(c, store) == 0
