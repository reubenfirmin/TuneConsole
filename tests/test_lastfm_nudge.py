"""Last.fm reminders follow the unlock notice by a week and retain their durable snooze."""
import pytest
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.web.routes.home_onboarding import lastfm_nudge_due, LASTFM_NUDGE_SNOOZE_S
from yt_playlist.providers import lastfm
from tests.conftest import FakeClient, seed_catalog_track
from tests.home_fixture_support import returning_listener

FIRST_PLAY = 120
REMINDER_AT = FIRST_PLAY + 8 * 86400


@pytest.fixture
def listener(store, monkeypatch):
    monkeypatch.setattr(lastfm, 'api_key', lambda source: source.settings.get_setting('lastfm_api_key') or None)
    account = store.accounts.upsert_account('main', 'credential', None, True)
    seed_catalog_track(store, 'song|artist', 'Song', 'Artist', video_id='song')
    store.history.record_play_event(account, 'song|artist', 'song', FIRST_PLAY)
    returning_listener(store, 'mixes')
    return store


def test_waits_one_week_after_initial_notice(listener):
    assert lastfm_nudge_due(listener, now=FIRST_PLAY + 86400) is False
    assert lastfm_nudge_due(listener, now=REMINDER_AT - 1) is False
    assert lastfm_nudge_due(listener, now=REMINDER_AT) is True


def test_not_due_without_live_listening(store):
    store.settings.set_setting('last_sync_at', '1')
    assert lastfm_nudge_due(store, now=REMINDER_AT) is False


def test_not_due_when_connected_even_while_enrichment_is_preparing(listener):
    listener.settings.set_setting('lastfm_api_key', 'controlled-key')
    assert lastfm_nudge_due(listener, now=REMINDER_AT) is False


def test_dismiss_snoozes_for_thirty_days_then_returns(listener):
    listener.settings.set_setting('lastfm_nudge_dismissed_at', str(REMINDER_AT))
    assert lastfm_nudge_due(listener, now=REMINDER_AT + LASTFM_NUDGE_SNOOZE_S - 1) is False
    assert lastfm_nudge_due(listener, now=REMINDER_AT + LASTFM_NUDGE_SNOOZE_S) is True


def test_unparseable_dismissal_timestamp_still_obeys_initial_delay(listener):
    listener.settings.set_setting('lastfm_nudge_dismissed_at', 'garbage')
    assert lastfm_nudge_due(listener, now=REMINDER_AT - 1) is False
    assert lastfm_nudge_due(listener, now=REMINDER_AT) is True


def test_reminder_and_dismissal_preserve_optional_setup_access(listener):
    store = listener
    c = TestClient(create_app(store, lambda: FakeClient(), now_fn=lambda: REMINDER_AT), base_url="http://127.0.0.1")
    html = c.get('/').text
    assert '/setup?tab=enrichment#setup-lastfm' in html
    assert 'id="lastfm-nudge"' in html
    assert 'Needs attention' in html
    assert 'Find more music that fits' in html and 'How to get here' in html
    assert 'Register for a free Last.fm API key, paste it into Setup and turn Last.fm on.' in html
    assert 'Set up Last.fm' in html
    assert c.post("/onboard/lastfm/dismiss").status_code == 200
    assert store.settings.get_setting("lastfm_nudge_dismissed_at") is not None   # snooze timestamp recorded
    after = c.get('/').text
    assert 'id="lastfm-nudge"' not in after
    assert 'Find more music that fits' in after
    assert '/setup?tab=enrichment' in after
    assert 'Register for a free Last.fm API key, paste it into Setup and turn Last.fm on.' in after
