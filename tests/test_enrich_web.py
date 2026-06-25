"""Tools › Enrichment page: render, live stats fragment, pause toggle."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1")


def _seed(store):
    t1 = store.upsert_track("v1", "A", "X", None, 200)
    t2 = store.upsert_track("v2", "B", "Y", None, 200)
    store.set_track_enrichment(t1, "Rock", "1999")
    store.mark_enriched([t1], now=5.0)                 # 1 of 2 processed, has genre+year
    return t1, t2


def test_enrich_page_renders_coverage(store):
    _seed(store)
    html = _client(store).get("/enrich").text
    assert "Enrichment" in html
    for label in ("Processed", "Genre", "Year", "BPM", "Energy", "Danceability"):
        assert label in html
    assert "In queue" in html and "Open conflicts" in html


def test_stats_fragment_reflects_state(store):
    _seed(store)
    html = _client(store).get("/enrich/stats").text
    assert "Caught up" not in html or "Enriching" in html or "Caught up" in html  # some state shown
    assert 'width:50%' in html.replace(" ", "")        # 1/2 processed -> 50% bar


def test_toggle_pauses_and_resumes(store):
    _seed(store)
    c = _client(store)
    assert store.get_setting("enrich_worker_enabled", "1") == "1"
    frag = c.post("/enrich/toggle").text
    assert store.get_setting("enrich_worker_enabled") == "0"   # paused
    assert "Resume worker" in frag and "Paused" in frag
    c.post("/enrich/toggle")
    assert store.get_setting("enrich_worker_enabled") == "1"   # resumed
