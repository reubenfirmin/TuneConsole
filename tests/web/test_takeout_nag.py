"""#61 Takeout nag: shows initially (no last_sync gate), re-nags every 90 days, terminal once an
import has actually landed matches (takeout_imported_at set)."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home import takeout_nag_due, TAKEOUT_NAG_SNOOZE_S
from tests.conftest import FakeClient


def test_due_initially_and_after_snooze_expiry(store):
    assert takeout_nag_due(store, now=1000.0) is True   # fresh install, nothing imported/dismissed yet
    store.set_setting("takeout_nag_dismissed_at", "1000.0")
    assert takeout_nag_due(store, now=1000.0 + 89 * 86400) is False   # still snoozed
    assert takeout_nag_due(store, now=1000.0 + 91 * 86400) is True    # snooze expired, due again


def test_terminal_once_imported(store):
    store.set_setting("takeout_nag_dismissed_at", "1000.0")
    store.set_setting("takeout_imported_at", "1000.0")
    # Even long past the snooze window, an actual import makes the nag terminal.
    assert takeout_nag_due(store, now=1000.0 + TAKEOUT_NAG_SNOOZE_S * 10) is False


def test_dismiss_route_stamps(store):
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "takeout-nag" in c.get("/").text
    assert c.post("/onboard/takeout/dismiss").status_code == 200
    assert store.get_setting("takeout_nag_dismissed_at") is not None   # snooze timestamp recorded
    assert "takeout-nag" not in c.get("/").text


def test_banner_shows_on_fresh_install_with_no_last_sync(store):
    # No last_sync gate: importing early is exactly the point, so the card must show immediately.
    assert store.get_setting("last_sync_at") is None
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "takeout-nag" in c.get("/").text


def test_banner_links_to_import_tab(store):
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    assert "/setup?tab=import" in c.get("/").text


def test_unparseable_dismissal_timestamp_means_due(store):
    store.set_setting("takeout_nag_dismissed_at", "garbage")
    assert takeout_nag_due(store, now=1000.0) is True


def test_fresh_install_shows_takeout_nag_but_not_gated_nudges(store):
    # The takeout nag deliberately escapes the first-sync gate; the Last.fm nudge and friends
    # must NOT escape with it (a prior fix hoisted the whole alerts include, un-gating them).
    assert store.get_setting("last_sync_at") is None
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    html = c.get("/").text
    assert "takeout-nag" in html
    assert "lastfm-nudge" not in html
