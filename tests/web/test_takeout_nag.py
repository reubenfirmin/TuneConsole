"""Optional history import follows onboarding, with a durable snooze and import retirement."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home_onboarding import takeout_nag_due, TAKEOUT_NAG_SNOOZE_S
from tests.conftest import FakeClient, seed_catalog_track
from tests.home_fixture_support import returning_listener

FIRST_PLAY = 120
REMINDER_AT = FIRST_PLAY + 9 * 86400


def _finished_onboarding(store):
    store.settings.set_setting("last_sync_at", "999")
    store.settings.set_setting("onboard_dismissed", "1")
    seed_catalog_track(store, 'song|artist', 'Song', 'Artist', video_id='existing-song')
    account = store.accounts.upsert_account('main', None, True)
    store.history.record_play_event(account, 'song|artist', 'existing-song', FIRST_PLAY)
    returning_listener(store, 'mixes')
    return account


def test_due_after_onboarding_and_after_snooze_expiry(store):
    assert takeout_nag_due(store, now=1000.0) is False
    store.settings.set_setting("last_sync_at", "999")
    assert takeout_nag_due(store, now=1000.0) is False
    _finished_onboarding(store)
    assert takeout_nag_due(store, now=FIRST_PLAY + 2 * 86400) is False
    assert takeout_nag_due(store, now=REMINDER_AT - 1) is False
    assert takeout_nag_due(store, now=REMINDER_AT) is True
    store.settings.set_setting('takeout_nag_dismissed_at', str(REMINDER_AT))
    assert takeout_nag_due(store, now=REMINDER_AT + TAKEOUT_NAG_SNOOZE_S - 1) is False
    assert takeout_nag_due(store, now=REMINDER_AT + TAKEOUT_NAG_SNOOZE_S) is True


def test_terminal_once_imported(store):
    _finished_onboarding(store)
    store.settings.set_setting("takeout_nag_dismissed_at", "1000.0")
    store.settings.set_setting("takeout_imported_at", "1000.0")
    # Even long past the snooze window, an actual import makes the nag terminal.
    assert takeout_nag_due(store, now=1000.0 + TAKEOUT_NAG_SNOOZE_S * 10) is False


def test_empty_upload_keeps_reminder_but_imported_play_suppresses_it(store):
    account = _finished_onboarding(store)
    store.history.record_takeout_import('empty-upload', account, REMINDER_AT, [], 0)
    assert takeout_nag_due(store, now=REMINDER_AT) is True
    store.history.import_play_events(account, [('song|artist', 'existing-song', 60)], source='takeout')
    assert takeout_nag_due(store, now=REMINDER_AT) is False


def test_dismiss_route_stamps(store):
    _finished_onboarding(store)
    c = TestClient(create_app(store, lambda: FakeClient(), now_fn=lambda: REMINDER_AT), base_url="http://127.0.0.1")
    notices = {'panel': 'notices', 'fragment': 'true'}
    response = c.get('/', params=notices)
    assert response.status_code == 200
    html = response.text
    assert 'id="takeout-nag"' in html
    assert 'aria-label="Needs attention"' in html
    assert 'href="/setup?tab=import#setup-takeout"' in html
    assert 'hx-post="/onboard/takeout/dismiss"' in html
    assert c.post("/onboard/takeout/dismiss").status_code == 200
    assert float(store.settings.get_setting('takeout_nag_dismissed_at')) == REMINDER_AT
    response = c.get('/', params=notices)
    assert response.status_code == 200
    assert 'id="takeout-nag"' not in response.text

    growth = c.get('/home/growth')
    assert growth.status_code == 200
    takeout_node = next(node for node in growth.json()['nodes'] if node['id'] == 'takeout')
    assert takeout_node['href'] == '/setup?tab=import#setup-takeout'
    assert 'Download your YouTube Music listening history from Google Takeout.' in takeout_node['how_to']


def test_banner_waits_on_fresh_install_and_during_onboarding(store):
    assert store.settings.get_setting("last_sync_at") is None
    c = TestClient(create_app(store, lambda: FakeClient()), base_url="http://127.0.0.1")
    html = c.get('/').text
    assert 'id="arrival-title"' in html and 'Welcome to TuneConsole' in html
    notices = {'panel': 'notices', 'fragment': 'true'}
    response = c.get('/', params=notices)
    assert response.status_code == 200
    assert 'id="takeout-nag"' not in response.text and 'id="lastfm-nudge"' not in response.text
    store.settings.set_setting("last_sync_at", "999")
    response = c.get('/', params=notices)
    assert response.status_code == 200
    assert 'id="takeout-nag"' not in response.text and 'id="lastfm-nudge"' not in response.text


def test_unparseable_dismissal_timestamp_means_due(store):
    _finished_onboarding(store)
    store.settings.set_setting("takeout_nag_dismissed_at", "garbage")
    assert takeout_nag_due(store, now=REMINDER_AT - 1) is False
    assert takeout_nag_due(store, now=REMINDER_AT) is True
