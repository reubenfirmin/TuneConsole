from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.home_fixture_support import (
    home_routes_only, publish_menu, returning_listener, without_metadata_providers,
)
from tests.store_fixture_support import (
    bounded_consumer_schema, native_sql_calls,
    uncontended_file_consumer_store as store, without_query_timing,
)


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing',
                                    'feedback_routes', 'without_metadata_providers')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'),), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def feedback_routes(monkeypatch, home_routes_only):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home_board, suggest
    from yt_playlist.web.routes.home_views import HomeViews

    def build(ctx):
        return [app_update.build(ctx), audio_preview.build(ctx),
                home_board.build(ctx, HomeViews(ctx)), suggest.build(ctx)]

    monkeypatch.setattr(webapp, 'build_all', build)


def _seed(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.tracks.upsert_track("v1", "Hit", "Fav", None, None)
    store.tracks.upsert_track("v2", "Bench", "Fav", None, None)   # deep cut -> appears in for_you
    now = 1000.0
    store.history.record_history_plays(iid, now - 100, ["hit|fav"], source_timezone="UTC")
    store.history.record_history_plays(iid, now - 50, ["hit|fav"], source_timezone="UTC")
    store.settings.set_setting("last_sync_at", str(now))     # synced -> Home renders the rec feed
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now,
                                     background_work=False),
                           base_url="http://127.0.0.1")


def test_home_proto_curation_not_pre_listen_taste_chips(store):
    # The Home proto-playlists are drafts you curate before listening, so each row offers a plain
    # "remove from this playlist", NOT taste feedback that only makes sense after hearing a track.
    # Cards are now lazy-loaded; assert against /home/cards instead of /.
    _, c = _seed(store)
    publish_menu(store, now=1000)
    returning_listener(store, 'mixes', now=1000)
    response = c.get("/home/cards")
    assert response.status_code == 200
    html = response.text
    assert "gen-rm" in html and "Remove from this playlist" in html
    for chip in ("More like this", "Not the vibe", "Wrong era", "Too mainstream", "Already know it"):
        assert chip not in html


def test_own_it_suppresses_without_taste_penalty(store):
    _, c = _seed(store)
    c.post("/recs/feedback", data={"item": "bench|fav", "surface": "for_you",
                                   "kind": "dismiss", "reason": "own_it", "lane": "deep_cut"})
    assert "bench|fav" in store.rec_model.suppressed_keys("for_you", now=1001.0)   # suppressed
    assert "lane:deep_cut" not in store.rec_model.get_weights()                    # but NO penalty


def test_less_nudges_lane_down(store):
    _, c = _seed(store)
    c.post("/recs/feedback", data={"item": "bench|fav", "surface": "for_you",
                                   "kind": "less", "reason": "vibe", "lane": "deep_cut"})
    # #85 _seed's now_fn is fixed at 1000.0; read at the same `now` so reversion (vs real wall-clock
    # time) doesn't erase the nudge before the assertion runs.
    assert store.rec_model.get_weights(now=1000.0).get("lane:deep_cut", 1.0) < 1.0


def test_mute_artist_via_chip(store):
    _, c = _seed(store)
    c.post("/recs/feedback", data={"item": "artist:Fav", "surface": "for_you", "kind": "mute"})
    assert "Fav" in store.rec_model.muted_artists()


def test_feedback_axis_routes_through_graduation_and_eventually_moves_weight(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app
    from tests.conftest import FakeClient
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")

    # Why-chip steering goes through the graduation ledger now (#43 / §4b), not a direct nudge: a single
    # event moves the ledger but leaves the permanent weight neutral (it is below theme_threshold).
    c.post("/recs/feedback", data={"item": "a|b", "kind": "less", "axis": "era:1990"})
    assert store.rec_model.get_weights(now=1.0).get("era:1990", 1.0) == 1.0     # one event: ledger only, weight untouched
    assert (store.rec_signals.get_theme("era:1990") or 0) < 0

    # Sustained feedback crosses the threshold and graduates the weight.
    for _ in range(3):
        c.post("/recs/feedback", data={"item": "a|b", "kind": "less", "axis": "era:1990"})
    # #85 the route's now_fn is fixed at 1.0; read at the same `now` so reversion (vs real wall-clock
    # time) doesn't erase the graduation before the assertion runs.
    assert store.rec_model.get_weights(now=1.0)["era:1990"] < 1.0               # 'less' graduates it down

    for _ in range(4):
        c.post("/recs/feedback", data={"item": "c|d", "kind": "more", "axis": "artist:Foo"})
    assert store.rec_model.get_weights(now=1.0)["artist:Foo"] > 1.0            # 'more' graduates it up
