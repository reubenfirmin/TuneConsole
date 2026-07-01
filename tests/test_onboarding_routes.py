import pytest
from fastapi.testclient import TestClient
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema(); return s


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient()
    return fake, TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: 1000.0),
                            base_url="http://127.0.0.1")


def _synced_thin(store):
    store.conn.execute("INSERT INTO tracks (identity_key, title, artist, video_id) "
                       "VALUES ('a|x','T','A','v1')")
    store.conn.execute("UPDATE tracks SET first_enriched_at=1 WHERE identity_key='a|x'")
    store.conn.commit()
    store.set_setting("last_sync_at", "999")   # makes sync.last_synced_ago truthy so home enters the synced branch


def test_home_shows_onboarding_when_thin(store):
    _synced_thin(store)
    _, c = _client(store)
    html = c.get("/").text
    assert "learning your taste" in html.lower()           # explainer copy present
    assert 'hx-get="/home/onboard/radio"' in html          # radio playlist lazy-loads


def test_onboard_radio_fragment_ok(store):
    _synced_thin(store)
    _, c = _client(store)
    assert c.get("/home/onboard/radio").status_code == 200


def test_onboard_done_dismisses(store):
    _synced_thin(store)
    _, c = _client(store)
    assert c.post("/onboard/done").status_code in (200, 204)
    assert store.get_setting("onboard_dismissed") == "1"


def test_onboarding_hides_taste_panel_and_mode_cards(store):
    _synced_thin(store)
    _, c = _client(store)
    html = c.get("/").text
    assert 'hx-get="/home/cards"' not in html               # mode-card row hidden during onboarding
    assert 'id="home-feed"' not in html                     # the normal feed partial is not included
