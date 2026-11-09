"""#79 Monthly recap: there is no Trends tab -- the reel is reached only via the Home nag card. Covers
the reel route, the absence of a browsable tab, and the Home recap nag (show / link / dismiss)."""
from datetime import datetime, timezone
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from starlette.testclient import TestClient

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from yt_playlist.rec.recap_details import slides
from tests.conftest import FakeClient
from tests.home_fixture_support import home_page_bytecode, returning_listener, without_metadata_providers
from tests.onboarding_support import publish_personal_mix
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, without_query_timing
from yt_playlist.core import schema_inspection


pytestmark = pytest.mark.usefixtures('without_query_timing', 'without_metadata_providers')


@pytest.fixture(scope='session')
def _pristine_current_schema():
    # Recap readers keep the full prepared story and dashboard menu; the only
    # catalog input used by their actual menu fixture is House.
    with bounded_consumer_schema((('house', 'House'),)) as schema:
        yield schema


@pytest.fixture(autouse=True)
def recap_routes(monkeypatch, home_page_bytecode):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home, trends

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (app_update, audio_preview, home, trends)])
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = home_page_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)

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
          "subtitle": "A newly heard artist", "detail": "7 of 10 tracks played", "heard": 7, "href": "/album?browse=MPRE"}]},
        {"label": "Playlists", "total": 1, "entries": [{"title": "September evenings",
          "detail": "24 songs · 12 plays over 3 days", "plays": 12, "days": 3,
          "href": "https://music.youtube.com/playlist?list=PL123"}]},
        {"label": "Artists", "total": 1, "entries": [{"title": "A newly heard artist",
          "detail": "8 plays over 3 days", "plays": 8, "days": 3, "href": "/artist?name=Artist"}]},
    ]},
    {"kind": "different", "title": "What you did differently", "findings": [
        {"metric": "activity", "title": "A quieter listening month",
         "detail": "75 plays this month. 2.5 a day, compared with your usual 9.0."},
        {"metric": "genre:jazz", "title": "More jazz",
         "detail": "25 plays over 5 days. 33% of your listening, compared with your usual 10%."},
    ]},
    {"kind": "same", "title": "What you did the same", "findings": [
        {"metric": "artist:Artist", "title": "Artist stayed in rotation",
         "detail": "20 plays over 5 days. You keep coming back."},
    ]},
]


def _detailed_story():
    return {**_STORY, "beats": slides([*_STORY["beats"][:-1], *_DETAIL_BEATS, _STORY["beats"][-1]])}


def test_optional_detail_screens_render_only_the_prepared_findings(_reel_client):
    story = _detailed_story()
    client, _ = _reel_client(story)
    html = client.get("/trends/story/2026-06").text
    assert html.count('class="slide slide-') == len(story["beats"])
    for kind in ("new_this_month", "different", "same"):
        assert f'class="slide slide-{kind}"' in html
    assert "7 of 10 tracks played" in html
    assert "75 plays this month" in html
    assert "Artist stayed in rotation" in html
    client, _ = _reel_client(_STORY)
    html = client.get("/trends/story/2026-06").text
    assert 'class="slide slide-new_this_month"' not in html
    assert 'class="slide slide-different"' not in html
    assert 'class="slide slide-same"' not in html


def test_cached_grouped_story_is_split_without_links_or_mutating_published_data(_client):
    from copy import deepcopy

    detail = deepcopy(_DETAIL_BEATS)
    detail[0]['groups'][1]['entries'].append({'title': 'Unused list', 'detail': '5 songs · 0 plays'})
    detail[0]['groups'][2]['entries'].append({'title': 'One-off artist', 'detail': '4 plays'})
    story = {**_STORY, 'beats': [*_STORY['beats'][:-1], *detail, _STORY['beats'][-1]]}
    client, store = _client(story)
    html = client.get('/trends/story/2026-06').text
    assert html.count('class="slide slide-new_this_month"') == 3
    assert html.count('class="slide slide-different"') == 2
    assert 'Unused list' not in html and 'One-off artist' not in html
    assert 'href="/album?' not in html and 'href="/artist?' not in html
    assert 'music.youtube.com/playlist' not in html
    assert store.rec_proposals.get_proposals('trend_rollups')['story'] == story


_NOW = datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp()


def _recap_app(store, story, synced=True, now_fn=lambda: _NOW):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    if synced:
        store.settings.set_setting("last_sync_at", "1700000000")
        publish_personal_mix(store, now=now_fn())
        returning_listener(store, 'mixes', now=now_fn())
    if story is not None:
        store.rec_proposals.put_proposals("trend_rollups", {"story": story}, 1000.0)
    return create_app(store, lambda: {iid: FakeClient()}, now_fn=now_fn)


@pytest.fixture
def _client(_pristine_current_schema):
    with ExitStack() as resources:
        def create(story, synced=True, now_fn=lambda: _NOW):
            store = Store(":memory:")
            resources.callback(store.conn.close)
            initialize_consumer(store, _pristine_current_schema)
            resources.enter_context(schema_inspection.reuse_tables(store.conn))
            app = _recap_app(store, story, synced=synced, now_fn=now_fn)
            client = resources.enter_context(TestClient(app, base_url="http://127.0.0.1"))
            return client, store

        yield create


@pytest.fixture
def _reel_client():
    from fastapi import FastAPI
    from yt_playlist.controllers.home_outputs import HomeOutputs
    from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
    from yt_playlist.controllers.settings import Settings
    from yt_playlist.repos.home_cache import HomeCacheRepo
    from yt_playlist.repos.settings import SettingsRepo
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import trends

    with ExitStack() as clients:
        def create(story):
            cache = Mock(spec_set=HomeCacheRepo)
            cache.document.return_value = (json.dumps({'story': story}) if story else None, None)
            home = HomeOutputs(cache, None, Mock(retained=nullcontext), None)
            settings = Mock(spec_set=SettingsRepo)
            store = SimpleNamespace(
                rec_proposals=RecommendationProposals(
                    None, None, None, None, None, home, None, None),
                settings=Settings(settings, None, None))
            templates = webapp.Jinja2Templates(directory=Path(webapp.__file__).with_name('templates'))
            templates.env.globals['asset_v'] = 'test'
            app = FastAPI()
            app.include_router(trends.build(SimpleNamespace(
                store=store, templates=templates, rec_worker=None)))
            return clients.enter_context(TestClient(app)), store

        yield create


def test_reel_renders_every_beat(_reel_client):
    c, store = _reel_client(_STORY)
    r = c.get("/trends/story/2026-06")
    assert r.status_code == 200
    assert r.text.count('class="slide slide-') == len(_STORY["beats"])
    assert "Daylight Trailblazer" in r.text and "Save image" in r.text
    assert "reel(" in r.text
    store.settings.repository.set_setting.assert_called_once_with('recap_viewed_month', '2026-06')


def test_cached_sparse_recap_cover_uses_activity_instead_of_old_hype(_client):
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
    assert store.rec_proposals.get_proposals("trend_rollups")["story"] == story


def test_reel_exits_land_on_the_dashboard_not_the_removed_trends_tab(_client):
    """#102 The reel's three exits (close, Escape, past-the-last-slide) all pointed at /trends, which
    was removed with the Trends tab: every way out of the recap dead-ended on a 404. Home is the only
    place the recap is reachable from, so it is where leaving it belongs."""
    c, _ = _client(_STORY)
    html = c.get("/trends/story/2026-06").text
    assert 'href="/trends"' not in html
    assert "'/trends'" not in html                  # the Escape and last-slide handlers
    assert 'class="reel-close" href="/"' in html
    assert html.count("window.location.href = '/'") == 2


def test_there_is_no_trends_tab(_client):
    c, _ = _client(_STORY)
    assert c.get("/trends").status_code == 404              # the browsable gallery is gone
    assert 'href="/trends"' not in c.get("/").text           # ...and there is no nav link to it


def test_home_recap_nag_links_into_the_reel(_client):
    c, _ = _client(_STORY)
    r = c.get("/")
    assert r.status_code == 200
    assert 'id="recap-nudge"' in r.text
    assert "Your June is wrapped" in r.text
    assert 'href="/trends/story/2026-06"' in r.text


def test_opening_recap_hides_notice_across_reloads_rebuilds_and_app_restarts(_client):
    c, store = _client(_STORY)
    # Seeing the notice on Home does not count as viewing the recap.
    for _ in range(2):
        assert 'id="recap-nudge"' in c.get("/").text
    assert store.settings.get_setting("recap_viewed_month") is None

    assert c.get("/trends/story/2026-06").status_code == 200
    assert store.settings.get_setting("recap_viewed_month") == "2026-06"
    assert 'id="recap-nudge"' not in c.get("/").text
    store.rec_proposals.put_proposals("trend_rollups", {"story": _STORY}, 2000.0)
    app = create_app(store, lambda: {}, now_fn=lambda: _NOW)
    with TestClient(app, base_url="http://127.0.0.1") as restarted:
        assert 'id="recap-nudge"' not in restarted.get("/").text
        # The direct recap link remains usable after viewing.
        assert restarted.get("/trends/story/2026-06").status_code == 200


def test_viewing_a_recap_does_not_hide_the_next_months_notice(_client):
    clock = [_NOW]
    c, store = _client(_STORY, now_fn=lambda: clock[0])
    assert c.get("/trends/story/2026-06").status_code == 200
    clock[0] = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    july = {**_STORY, "month": "2026-07", "month_name": "July"}
    store.rec_proposals.put_proposals("trend_rollups", {"story": july}, 2000.0)
    assert 'id="recap-nudge"' in c.get("/").text
    assert c.get("/trends/story/2026-07").status_code == 200
    assert 'id="recap-nudge"' not in c.get("/").text


def test_missing_recap_does_not_change_viewed_month():
    from fastapi import FastAPI
    from yt_playlist.controllers.home_outputs import HomeOutputs
    from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
    from yt_playlist.controllers.settings import Settings
    from yt_playlist.repos.home_cache import HomeCacheRepo
    from yt_playlist.repos.settings import SettingsRepo
    from yt_playlist.web.routes import trends

    for story, month, viewed in ((_STORY, '1999-01', '2026-05'), (None, '2026-06', None)):
        cache = Mock(spec_set=HomeCacheRepo)
        cache.document.return_value = (json.dumps({'story': story}) if story else None, None)
        home = HomeOutputs(cache, None, Mock(retained=nullcontext), None)
        settings = Mock(spec_set=SettingsRepo)
        settings.get_setting.return_value = viewed
        store = SimpleNamespace(
            rec_proposals=RecommendationProposals(
                None, None, None, None, None, home, None, None),
            settings=Settings(settings, None, None))
        templates = Mock()
        app = FastAPI()
        app.include_router(trends.build(SimpleNamespace(store=store, templates=templates)))

        with TestClient(app) as client:
            assert client.get(f'/trends/story/{month}').status_code == 404
        assert store.settings.get_setting('recap_viewed_month') == viewed
        assert settings.mock_calls == [call.get_setting('recap_viewed_month', None)]
        cache.document.assert_called_once_with('trend_rollups')
        templates.TemplateResponse.assert_not_called()


def test_home_recap_nag_hidden_once_dismissed(_client):
    clock = [_NOW]
    c, store = _client(_STORY, now_fn=lambda: clock[0])
    assert 'id="recap-nudge"' in c.get("/").text
    store.settings.set_setting("recap_dismissed_month", "2026-06")
    assert "recap-nudge" not in c.get("/").text
    # a later month's recap re-shows (the dismissal is per-month)
    clock[0] = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    store.rec_proposals.put_proposals("trend_rollups", {"story": {**_STORY, "month": "2026-07", "month_name": "July"}}, 1000.0)
    assert 'id="recap-nudge"' in c.get("/").text


@pytest.mark.parametrize("month", ["2026-07", "2026-08", "2026-10"])
def test_home_hides_cached_recap_outside_the_previous_calendar_month(_client, month):
    october = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
    c, _ = _client({**_STORY, "month": month}, now_fn=lambda: october)
    assert 'id="recap-nudge"' not in c.get("/").text


def test_home_recap_expires_at_rollover_without_waiting_for_a_worker_rebuild(_client):
    clock = [datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()]
    c, _ = _client(_STORY, now_fn=lambda: clock[0])
    assert 'id="recap-nudge"' in c.get("/").text
    clock[0] += 1
    assert 'id="recap-nudge"' not in c.get("/").text


def test_home_offers_september_in_october(_client):
    october = datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
    c, _ = _client({**_STORY, "month": "2026-09", "month_name": "September"}, now_fn=lambda: october)
    html = c.get("/").text
    assert "Your September is wrapped" in html
    assert 'href="/trends/story/2026-09"' in html


def test_recap_dismiss_route_records_the_month(_client):
    c, store = _client(_STORY)
    assert c.post("/onboard/recap/dismiss?month=2026-06").status_code == 200
    assert store.settings.get_setting("recap_dismissed_month") == "2026-06"
