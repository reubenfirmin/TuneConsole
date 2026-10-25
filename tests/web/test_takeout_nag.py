"""Optional history import follows onboarding, with a durable snooze and import retirement."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home_onboarding import takeout_nag_due, TAKEOUT_NAG_SNOOZE_S
from tests.conftest import FakeClient


def _finished_onboarding(store):
    store.settings.set_setting("last_sync_at", "999")
    store.settings.set_setting("onboard_dismissed", "1")


def test_due_after_onboarding_and_after_snooze_expiry(store):
    assert takeout_nag_due(store, now=1000.0) is False
    store.settings.set_setting("last_sync_at", "999")
    assert takeout_nag_due(store, now=1000.0) is False
    _finished_onboarding(store)
    assert takeout_nag_due(store, now=1000.0) is True
    store.settings.set_setting("takeout_nag_dismissed_at", "1000.0")
    assert takeout_nag_due(store, now=1000.0 + 89 * 86400) is False   # still snoozed
    assert takeout_nag_due(store, now=1000.0 + 91 * 86400) is True    # snooze expired, due again


def test_terminal_once_imported(store):
    _finished_onboarding(store)
    store.settings.set_setting("takeout_nag_dismissed_at", "1000.0")
    store.settings.set_setting("takeout_imported_at", "1000.0")
    # Even long past the snooze window, an actual import makes the nag terminal.
    assert takeout_nag_due(store, now=1000.0 + TAKEOUT_NAG_SNOOZE_S * 10) is False


def test_dismiss_route_stamps(store):
    _finished_onboarding(store)
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "takeout-nag" in c.get("/").text
    assert c.post("/onboard/takeout/dismiss").status_code == 200
    assert store.settings.get_setting("takeout_nag_dismissed_at") is not None   # snooze timestamp recorded
    assert "takeout-nag" not in c.get("/").text


def test_banner_waits_on_fresh_install_and_during_onboarding(store):
    assert store.settings.get_setting("last_sync_at") is None
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "takeout-nag" not in c.get("/").text
    store.settings.set_setting("last_sync_at", "999")
    assert "takeout-nag" not in c.get("/").text


def test_banner_links_to_import_tab(store):
    _finished_onboarding(store)
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    html = c.get("/").text
    assert "/setup?tab=import" in html
    assert "Want better recommendations?" in html


def test_unparseable_dismissal_timestamp_means_due(store):
    _finished_onboarding(store)
    store.settings.set_setting("takeout_nag_dismissed_at", "garbage")
    assert takeout_nag_due(store, now=1000.0) is True


def test_fresh_install_shows_welcome_before_optional_nudges(store):
    assert store.settings.get_setting("last_sync_at") is None
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    html = c.get("/").text
    assert "How do you like to listen?" in html
    assert "takeout-nag" not in html
    assert "lastfm-nudge" not in html
