"""#79 Monthly recap: there is no Trends tab -- the reel is reached only via the Home nag card. Covers
the reel route, the absence of a browsable tab, and the Home recap nag (show / link / dismiss)."""
from starlette.testclient import TestClient

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

_AURA = {"hue": "#8b7cff", "turbulence": 0.5, "reach": 0.6, "warmth": 0.4}
_STORY = {
    "month": "2026-06", "month_name": "June",
    "beats": [
        {"kind": "cover", "month_name": "June", "plays": 535,
         "thesis": "The month you cast a wide net.", "personality": "Daylight Trailblazer"},
        {"kind": "numbers", "plays": 535, "listen_days": 19, "streak": 14, "distinct_artists": 300},
        {"kind": "wide_net", "artists": 300, "plays": 535},
        {"kind": "personality", "name": "Daylight Trailblazer", "blurb": "high-energy and curious",
         "axes": {"energy": 0.5, "exploration": 0.6, "rhythm": 0.4}, "aura": _AURA, "top_family": "house"},
        {"kind": "closing", "month_name": "June", "plays": 535, "personality": "Daylight Trailblazer",
         "aura": _AURA, "top_artist": "Ben Böhmer", "listen_days": 19},
    ],
}


def _client(story, synced=True):
    store = Store(":memory:")
    store.init_schema()
    iid = store.upsert_identity("main", "cred", None, True)
    if synced:
        store.set_setting("last_sync_at", "1700000000")
    if story is not None:
        store.put_proposals("trend_rollups", {"story": story}, 1000.0)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_reel_renders_every_beat():
    c, _ = _client(_STORY)
    r = c.get("/trends/story/2026-06")
    assert r.status_code == 200
    assert r.text.count('class="slide slide-') == len(_STORY["beats"])
    assert "Daylight Trailblazer" in r.text and "Save image" in r.text
    assert "reel(" in r.text


def test_reel_404_for_unknown_or_missing():
    c, _ = _client(_STORY)
    assert c.get("/trends/story/1999-01").status_code == 404
    c2, _ = _client(None)
    assert c2.get("/trends/story/2026-06").status_code == 404


def test_there_is_no_trends_tab():
    c, _ = _client(_STORY)
    assert c.get("/trends").status_code == 404              # the browsable gallery is gone
    assert 'href="/trends"' not in c.get("/").text           # ...and there is no nav link to it


def test_home_recap_nag_links_into_the_reel():
    c, _ = _client(_STORY)
    r = c.get("/")
    assert r.status_code == 200
    assert 'id="recap-nudge"' in r.text
    assert "Your June is wrapped" in r.text
    assert 'href="/trends/story/2026-06"' in r.text


def test_home_recap_nag_hidden_once_dismissed():
    c, store = _client(_STORY)
    assert 'id="recap-nudge"' in c.get("/").text
    store.set_setting("recap_dismissed_month", "2026-06")
    assert "recap-nudge" not in c.get("/").text
    # a later month's recap re-shows (the dismissal is per-month)
    store.put_proposals("trend_rollups", {"story": {**_STORY, "month": "2026-07", "month_name": "July"}}, 1000.0)
    assert 'id="recap-nudge"' in c.get("/").text


def test_recap_dismiss_route_records_the_month():
    c, store = _client(_STORY)
    assert c.post("/onboard/recap/dismiss?month=2026-06").status_code == 200
    assert store.get_setting("recap_dismissed_month") == "2026-06"
