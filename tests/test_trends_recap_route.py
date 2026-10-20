"""#79 Monthly recap: there is no Trends tab -- the reel is reached only via the Home nag card. Covers
the reel route, the absence of a browsable tab, and the Home recap nag (show / link / dismiss)."""
from datetime import datetime, timezone

import pytest
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


_DETAIL_BEATS = [
    {"kind": "new_this_month", "title": "New this month", "groups": [
        {"label": "Albums", "total": 1, "entries": [{"title": "An album with a long title",
          "subtitle": "A newly heard artist", "detail": "7 of 10 tracks played", "href": "/album?browse=MPRE"}]},
        {"label": "Playlists", "total": 1, "entries": [{"title": "September evenings",
          "detail": "24 songs · 12 recorded plays", "href": "https://music.youtube.com/playlist?list=PL123"}]},
        {"label": "Artists", "total": 1, "entries": [{"title": "A newly heard artist",
          "detail": "8 plays", "href": "/artist?name=Artist"}]},
    ], "note": "First heard in your recorded history. Album counts are distinct tracks played."},
    {"kind": "different", "title": "What you did differently", "findings": [
        {"metric": "activity", "title": "A quieter listening month",
         "detail": "75 recorded plays; 2.5 per day, compared with your usual 9.0."},
        {"metric": "genre:jazz", "title": "More jazz",
         "detail": "25 plays across 5 days (33% of plays); usually 10%."},
    ], "note": "Compared with the median of 3 comparable months in the previous six."},
    {"kind": "same", "title": "What you did the same", "findings": [
        {"metric": "artist:Artist", "title": "Artist stayed in rotation",
         "detail": "20 plays across 5 days (27% of plays); usually 25%."},
    ], "note": "Compared with the median of 3 comparable months in the previous six."},
]


def _detailed_story():
    return {**_STORY, "beats": [*_STORY["beats"][:-1], *_DETAIL_BEATS, _STORY["beats"][-1]]}


def test_optional_detail_screens_render_only_the_prepared_findings():
    story = _detailed_story()
    client, _ = _client(story)
    html = client.get("/trends/story/2026-06").text
    assert html.count('class="slide slide-') == len(story["beats"])
    for kind in ("new_this_month", "different", "same"):
        assert f'class="slide slide-{kind}"' in html
    assert "7 of 10 tracks played" in html
    assert "75 recorded plays" in html
    assert "Artist stayed in rotation" in html
    client, _ = _client(_STORY)
    html = client.get("/trends/story/2026-06").text
    assert 'class="slide slide-new_this_month"' not in html
    assert 'class="slide slide-different"' not in html
    assert 'class="slide slide-same"' not in html


_NOW = datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp()


def _client(story, synced=True, now_fn=lambda: _NOW):
    store = Store(":memory:")
    store.init_schema()
    iid = store.upsert_identity("main", "cred", None, True)
    if synced:
        store.set_setting("last_sync_at", "1700000000")
    if story is not None:
        store.put_proposals("trend_rollups", {"story": story}, 1000.0)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=now_fn)
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_reel_renders_every_beat():
    c, _ = _client(_STORY)
    r = c.get("/trends/story/2026-06")
    assert r.status_code == 200
    assert r.text.count('class="slide slide-') == len(_STORY["beats"])
    assert "Daylight Trailblazer" in r.text and "Save image" in r.text
    assert "reel(" in r.text


def test_cached_sparse_recap_cover_uses_activity_instead_of_old_hype():
    story = {**_STORY, "month": "2026-09", "month_name": "September", "beats": [
        {"kind": "cover", "month_name": "September", "plays": 75,
         "thesis": "The month you went all in."},
        {"kind": "numbers", "plays": 75, "listen_days": 5, "streak": 1},
        {"kind": "all_in", "artist": "A", "plays": 30, "share": 0.4},
    ]}
    c, store = _client(story)
    html = c.get("/trends/story/2026-09").text
    assert "Your September" in html
    assert "75 plays across 5 listening days." in html
    assert "The month you went all in." not in html
    assert 'class="slide slide-all_in"' in html
    assert store.get_proposals("trend_rollups")["story"] == story


def test_reel_exits_land_on_the_dashboard_not_the_removed_trends_tab():
    """#102 The reel's three exits (close, Escape, past-the-last-slide) all pointed at /trends, which
    was removed with the Trends tab: every way out of the recap dead-ended on a 404. Home is the only
    place the recap is reachable from, so it is where leaving it belongs."""
    c, _ = _client(_STORY)
    html = c.get("/trends/story/2026-06").text
    assert 'href="/trends"' not in html
    assert "'/trends'" not in html                  # the Escape and last-slide handlers
    assert 'class="reel-close" href="/"' in html
    assert html.count("window.location.href = '/'") == 2


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


def test_opening_recap_hides_notice_across_reloads_rebuilds_and_app_restarts():
    c, store = _client(_STORY)
    # Seeing the notice on Home does not count as viewing the recap.
    for _ in range(2):
        assert 'id="recap-nudge"' in c.get("/").text
    assert store.get_setting("recap_viewed_month") is None

    assert c.get("/trends/story/2026-06").status_code == 200
    assert store.get_setting("recap_viewed_month") == "2026-06"
    assert 'id="recap-nudge"' not in c.get("/").text
    store.put_proposals("trend_rollups", {"story": _STORY}, 2000.0)
    app = create_app(store, lambda: {}, now_fn=lambda: _NOW)
    restarted = TestClient(app, base_url="http://127.0.0.1")
    assert 'id="recap-nudge"' not in restarted.get("/").text
    # The direct recap link remains usable after viewing.
    assert restarted.get("/trends/story/2026-06").status_code == 200


def test_viewing_a_recap_does_not_hide_the_next_months_notice():
    clock = [_NOW]
    c, store = _client(_STORY, now_fn=lambda: clock[0])
    assert c.get("/trends/story/2026-06").status_code == 200
    clock[0] = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    july = {**_STORY, "month": "2026-07", "month_name": "July"}
    store.put_proposals("trend_rollups", {"story": july}, 2000.0)
    assert 'id="recap-nudge"' in c.get("/").text
    assert c.get("/trends/story/2026-07").status_code == 200
    assert 'id="recap-nudge"' not in c.get("/").text


def test_missing_recap_does_not_change_viewed_month():
    c, store = _client(_STORY)
    store.set_setting("recap_viewed_month", "2026-05")
    assert c.get("/trends/story/1999-01").status_code == 404
    assert store.get_setting("recap_viewed_month") == "2026-05"
    c2, store2 = _client(None)
    assert c2.get("/trends/story/2026-06").status_code == 404
    assert store2.get_setting("recap_viewed_month") is None


def test_home_recap_nag_hidden_once_dismissed():
    clock = [_NOW]
    c, store = _client(_STORY, now_fn=lambda: clock[0])
    assert 'id="recap-nudge"' in c.get("/").text
    store.set_setting("recap_dismissed_month", "2026-06")
    assert "recap-nudge" not in c.get("/").text
    # a later month's recap re-shows (the dismissal is per-month)
    clock[0] = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    store.put_proposals("trend_rollups", {"story": {**_STORY, "month": "2026-07", "month_name": "July"}}, 1000.0)
    assert 'id="recap-nudge"' in c.get("/").text


@pytest.mark.parametrize("month", ["2026-07", "2026-08", "2026-10"])
def test_home_hides_cached_recap_outside_the_previous_calendar_month(month):
    october = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
    c, _ = _client({**_STORY, "month": month}, now_fn=lambda: october)
    assert 'id="recap-nudge"' not in c.get("/").text


def test_home_recap_expires_at_rollover_without_waiting_for_a_worker_rebuild():
    clock = [datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()]
    c, _ = _client(_STORY, now_fn=lambda: clock[0])
    assert 'id="recap-nudge"' in c.get("/").text
    clock[0] += 1
    assert 'id="recap-nudge"' not in c.get("/").text


def test_home_offers_september_in_october():
    october = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
    c, _ = _client({**_STORY, "month": "2026-09", "month_name": "September"}, now_fn=lambda: october)
    html = c.get("/").text
    assert "Your September is wrapped" in html
    assert 'href="/trends/story/2026-09"' in html


def test_recap_dismiss_route_records_the_month():
    c, store = _client(_STORY)
    assert c.post("/onboard/recap/dismiss?month=2026-06").status_code == 200
    assert store.get_setting("recap_dismissed_month") == "2026-06"
