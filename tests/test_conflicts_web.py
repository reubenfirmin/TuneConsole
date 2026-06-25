"""The conflict header icon + resolver modal + resolution overwrite (playlist scope)."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    return TestClient(app, base_url="http://127.0.0.1"), iid


def _seed(store, iid):
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    tid = store.upsert_track("v0", "Hyperballad", "Bjork", "Post", 200, 1)
    store.set_playlist_tracks(a, [tid])
    store.set_track_enrichment(tid, "Electronic", "1995")     # canonical = MusicBrainz's pick
    store.upsert_conflict(tid, "genre", [{"provider": "musicbrainz", "value": "Electronic"},
                                         {"provider": "discogs", "value": "Art Pop"}])
    return a, tid


def test_page_passes_conflict_count_to_panel(store):
    c, iid = _client(store)
    a, _tid = _seed(store, iid)
    html = c.get(f"/playlist/{a}").text
    assert f"enrichPanel({a}, false, 0, '', null, 1)" in html   # conflict_count = 1 lights the icon


def test_resolver_lists_candidates(store):
    c, iid = _client(store)
    a, _tid = _seed(store, iid)
    html = c.get(f"/playlist/{a}/conflicts").text
    assert "Hyperballad" in html
    assert "Electronic" in html and "Art Pop" in html
    assert "musicbrainz" in html and "discogs" in html


def test_resolve_overwrites_column_and_clears_conflict(store):
    c, iid = _client(store)
    a, tid = _seed(store, iid)
    r = c.post(f"/playlist/{a}/conflicts/resolve", data={f"{tid}:genre": "Art Pop"})
    assert r.status_code == 200
    # canonical column overwritten with the user's pick...
    assert store.conn.execute("SELECT genre FROM tracks WHERE id=?", (tid,)).fetchone()["genre"] == "Art Pop"
    # ...and the conflict no longer counts
    assert store.conflict_count_for_playlist(a) == 0
    assert "Nothing left to resolve" in r.text
