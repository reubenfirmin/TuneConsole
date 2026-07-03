from fastapi.testclient import TestClient

from yt_playlist.rec import recommend
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                           base_url="http://127.0.0.1")


def test_no_alerts_when_nothing_to_do(store):
    # When everything is triaged, the alerts section renders nothing at all (an empty "all clear"
    # note is just noise, see _partials/alerts.html). The onboarding nudges (welcome, Last.fm key,
    # identities merge) count as "to do" until dismissed, so dismiss them here.
    _, c = _client(store)
    store.set_setting("last_sync_at", "1000.0")     # not stale
    store.set_setting("intro_dismissed", "1")
    store.set_setting("identities_nudge_dismissed", "1")
    store.set_setting("lastfm_nudge_dismissed_at", "1000.0")   # snoozed for 30 days
    store.set_setting("takeout_imported_at", "1000.0")         # #61 nag is terminal once imported
    html = c.get("/").text
    assert "alert-card" not in html
    assert "All clear" not in html


def test_alert_dismiss_persists(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PE", "Empties", 0, "h", 0.0)   # an empty-playlist alert
    recommend.refresh_cleanup(store, now=1000.0)               # materialize the cached cleanup summary
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                   base_url="http://127.0.0.1")
    assert any(a.kind == "cleanup" for a in recommend.take_action(store, 1000.0, {}))
    c.post("/recs/feedback", data={"item": "cleanup:all", "surface": "alert", "kind": "not_now"})
    # snoozed -> take_action no longer surfaces it
    assert not any(a.key == "cleanup:all" for a in recommend.take_action(store, 1001.0, {}))


def test_transparency_note_when_muted(store):
    _, c = _client(store)
    store.set_setting("last_sync_at", "1.0")        # synced -> the gated muted-artist note renders
    store.set_setting("onboard_dismissed", "1")     # graduated user: testing the normal feed, not onboarding
    store.record_feedback("for_you", "artist:Coldplay", "mute", now=1.0)
    assert "muted artist" in c.get("/").text and "Taste Model" in c.get("/").text
