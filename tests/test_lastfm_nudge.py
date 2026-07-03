"""The home-page Last.fm key nudge: due whenever no key is set, and dismissing snoozes it for
30 days (lastfm_nudge_dismissed_at timestamp) so a long-lived install gets reminded again."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home import lastfm_nudge_due, LASTFM_NUDGE_SNOOZE_S
from yt_playlist.providers import lastfm
from tests.conftest import FakeClient


def test_due_when_no_key_and_not_snoozed(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    assert lastfm_nudge_due(store) is True


def test_not_due_when_key_present(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: True)
    assert lastfm_nudge_due(store) is False


def test_dismiss_snoozes_for_thirty_days_then_returns(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    store.set_setting("lastfm_nudge_dismissed_at", "1000.0")
    assert lastfm_nudge_due(store, now=1000.0 + LASTFM_NUDGE_SNOOZE_S - 1) is False   # snoozed
    assert lastfm_nudge_due(store, now=1000.0 + LASTFM_NUDGE_SNOOZE_S) is True        # reminded again


def test_unparseable_dismissal_timestamp_means_due(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    store.set_setting("lastfm_nudge_dismissed_at", "garbage")
    assert lastfm_nudge_due(store, now=1000.0) is True


def test_banner_links_to_enrichment_tab(store, monkeypatch):
    # #69: the card's CTA must deep-link to the setup page's Enrichment tab (where the key lives),
    # not dump the user on the default Pairing tab.
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    store.set_setting("last_sync_at", "1700000000")
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "/setup?tab=enrichment" in c.get("/").text


def test_banner_renders_and_dismiss_persists(store, monkeypatch):
    monkeypatch.setattr(lastfm, "available", lambda s=None: False)
    store.set_setting("last_sync_at", "1700000000")
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "lastfm-nudge" in c.get("/").text
    assert c.post("/onboard/lastfm/dismiss").status_code == 200
    assert store.get_setting("lastfm_nudge_dismissed_at") is not None   # snooze timestamp recorded
    assert "lastfm-nudge" not in c.get("/").text
