"""Contract tests for the htmx playlist-detail routes (rename / track-year / track-genre /
remove-track / reorder / alternates / add-tracks / lastfm-key).

These routes now consume form data and return _partials fragments (or HX-Refresh), instead of
the old JSON payloads. Coverage moved here from the JSON-based tests in test_web.py as each
route is converted.
"""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


# --- rename ---

def test_rename_returns_head_fragment(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Old Name", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post(f"/playlist/{a}/rename", data={"title": "  New Name  "})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()        # a fragment, not the whole page
    assert "New Name" in r.text and 'id="pl-title"' in r.text
    assert store.get_playlist(a).title == "New Name"       # trimmed + persisted
    assert fc.edited == [("PL1", {"title": "New Name"})]   # pushed to YouTube


def test_rename_empty_returns_toast(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Keep", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post(f"/playlist/{a}/rename", data={"title": "   "})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.get_playlist(a).title == "Keep"           # unchanged


# --- track genre / year (shared track_row fragment) ---

def _seed_one_track(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S0", "X", "Al", 200, 1)])
    return iid, a


def test_track_genre_returns_row_fragment(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-genre", data={"video_id": "v0", "genre": "Jazz"})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()
    assert 'class="srow"' in r.text and 'data-vid="v0"' in r.text   # the <tr> swap unit
    assert "Jazz" in r.text                                          # rendered into the genre cell
    assert {t["video_id"]: t["genre"] for t in store.playlist_tracks_detail(a)} == {"v0": "Jazz"}


def test_track_genre_no_vid_returns_toast(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-genre", data={"genre": "Jazz"})
    assert r.status_code == 422 and 'hx-swap-oob="afterbegin:#toasts"' in r.text


def test_track_year_returns_row_fragment(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-year", data={"video_id": "v0", "year": "1991"})
    assert r.status_code == 200
    assert 'data-vid="v0"' in r.text and "1991" in r.text
    assert store.playlist_tracks_detail(a)[0]["year"] == "1991"


def test_enrich_track_events_carry_rendered_row(store, monkeypatch):
    import json as _json
    import yt_playlist.musicbrainz as mb
    monkeypatch.setattr(mb, "enrich", lambda title, artist: ("Rock", "1998") if title == "S0" else (None, None))
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    jid = c.post(f"/playlist/{a}/enrich/musicbrainz").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    track_evs = [_json.loads(l[6:]) for l in body.splitlines()
                 if l.startswith("data: ") and '"type": "track"' in l]
    assert track_evs and all("row_html" in e for e in track_evs)     # server-rendered cell HTML
    hit = next(e for e in track_evs if e["video_id"] == "v0")
    assert 'class="srow"' in hit["row_html"] and "Rock" in hit["row_html"] and "1998" in hit["row_html"]
