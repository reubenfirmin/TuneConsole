import json
from html.parser import HTMLParser
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


class _TrackInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.value = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "track":
            self.value = attrs.get("value")


def _seed(store, genre="Techno"):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Anchor", "Band", None, None)
    b = store.upsert_track("v2", "Bonus", "Band", None, None)
    if genre:
        store.set_track_genre(a, genre)
        store.set_track_genre(b, genre)
    target = store.upsert_playlist(iid, "PT", "Target", 1, "h", 0.0)
    store.set_playlist_tracks(target, [a])
    other = store.upsert_playlist(iid, "PO", "Other", 2, "h2", 0.0)
    store.set_playlist_tracks(other, [a, b])
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    return target, TestClient(app, base_url="http://127.0.0.1")


def test_playlist_suggestions_fragment_renders_fits(store):
    target, c = _seed(store)
    r = c.get(f"/playlist/{target}/suggestions")
    assert r.status_code == 200
    assert "Complete this playlist" in r.text
    assert "Bonus" in r.text                       # a fitting owned track


def test_playlist_suggestions_unknown_id_404(store):
    _, c = _seed(store)
    assert c.get("/playlist/999999/suggestions").status_code == 404


def test_playlist_page_lazy_loads_suggestions(store):
    target, c = _seed(store)
    assert f"/playlist/{target}/suggestions" in c.get(f"/playlist/{target}").text


def test_suggestion_card_has_wired_add_button(store):
    target, c = _seed(store)
    frag = c.get(f"/playlist/{target}/suggestions").text
    assert f"/playlist/{target}/add-tracks" in frag    # Add posts to the existing endpoint
    assert 'name="track"' in frag and "+ Add" in frag and "v2" in frag
    parser = _TrackInputParser()
    parser.feed(frag)
    assert json.loads(parser.value)["videoId"] == "v2"  # browser receives the complete JSON value


def test_recs_rebuild_endpoint(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/recs/rebuild")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_add_suggested_track_adds_it_to_the_playlist(store):
    target, c = _seed(store)
    # what the Add button POSTs for the "Bonus" suggestion (videoId v2): a 'track' form field
    r = c.post(f"/playlist/{target}/add-tracks",
               data={"track": json.dumps({"videoId": "v2", "title": "Bonus", "artist": "Band"})})
    assert r.status_code == 200
    assert "bonus|band" in store.get_playlist_track_keys(target)


def test_suggestion_add_appends_row_without_refresh(store):
    target, c = _seed(store)
    track = json.dumps({"videoId": "v2", "title": "Bonus", "artist": "Band"})
    r = c.post(f"/playlist/{target}/add-tracks", data={"track": track, "suggestion": "1"})
    assert r.status_code == 200
    assert "HX-Refresh" not in r.headers
    assert 'class="suggestion-add-result"' in r.text
    assert 'class="suggestion-track-row"' in r.text
    assert 'data-vid="v2"' in r.text
    assert 'data-track-count="2"' in r.text


def test_genreless_suggestion_add_requeues_enrichment(store):
    target, c = _seed(store, genre=None)
    tid = store.track_ids_for_videos(["v2"])["v2"]
    store.mark_enriched([tid], now=5.0)  # an earlier provider miss would otherwise wait 30 days

    class Worker:
        triggered = 0

        def trigger(self):
            self.triggered += 1

    worker = Worker()
    c.app.state.ctx.enrich_worker = worker
    track = json.dumps({"videoId": "v2", "title": "Bonus", "artist": "Band"})
    r = c.post(f"/playlist/{target}/add-tracks", data={"track": track, "suggestion": "1"})

    assert r.status_code == 200 and worker.triggered == 1
    row = store.conn.execute(
        "SELECT first_enriched_at, last_enriched_at FROM tracks WHERE id=?", (tid,)).fetchone()
    assert row["first_enriched_at"] is None and row["last_enriched_at"] is None


def test_suggestion_dismiss_is_reasoned(store):
    target, c = _seed(store)
    frag = c.get(f"/playlist/{target}/suggestions").text
    # the × opens reason chips that route to /recs/feedback with a reason (not a bare dismiss)
    assert "/recs/feedback" in frag
    assert "wrong era" in frag and "already know it" in frag and "not this artist" in frag
    assert '"reason":"era"' in frag and '"reason":"own_it"' in frag
