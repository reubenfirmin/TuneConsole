"""Taste feedback summaries, track bans and unban routes."""
import json
from html.parser import HTMLParser
import pytest

from yt_playlist.core.store import Store
from yt_playlist.library import ratings
from tests.taste_support import _client

def test_taste_page_lists_and_clears_bans(store):
    store.tracks.upsert_track("banned-video", "Song", "Band", "An Album", None,
                       thumbnail="/static/banned-cover.jpg")
    store.rec_model.record_dislike("song|band", until=99999.0, now=1.0)
    c = _client(store)
    html = c.get("/taste/panels/feedback").text
    assert '<table class="taste-feedback-table"' in html
    assert "Song" in html and "Band" in html and "An Album" in html
    assert 'src="/static/banned-cover.jpg"' in html
    assert "https://music.youtube.com/watch?v=banned-video" in html
    assert c.post("/taste/unban", data={"key": "song|band"}).status_code == 200
    assert store.rec_model.disliked_identity_keys() == set()
    assert "No banned tracks." in c.get("/taste/panels/feedback").text


def test_feedback_tracks_resolve_discovery_and_keep_missing_metadata_manageable(store):
    store.discovery.upsert_discovered_track("new song|new artist", "discovered-video", "New Song", "New Artist",
                                  "New Album", "/static/discovered-cover.jpg", None, None, "", now=1.0)
    store.rec_model.record_dislike("new song|new artist", until=None, now=2.0)
    store.rec_model.record_dislike("removed song|former artist", until=0.0, now=3.0)
    response = _client(store).get("/taste/panels/feedback")
    assert response.status_code == 200
    tracks = response.context["bans"]
    assert [t["title"] for t in tracks] == ["removed song", "New Song"]  # newest first
    assert tracks[0]["artist"] == "former artist" and tracks[0]["video_id"] is None
    assert tracks[0]["until_label"] == "Expired"
    assert tracks[1]["thumbnail"] == "/static/discovered-cover.jpg"
    assert "No expiry" in response.text
    assert 'href="https://music.youtube.com/watch?v=None"' not in response.text


def test_feedback_unban_preserves_quoted_track_keys(store):
    class UnbanButtons(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button" and attrs.get("hx-post") == "/taste/unban":
                self.values.append(json.loads(attrs["hx-vals"]))

    key = '''song "live" & <rare>|artist's band'''
    store.rec_model.record_dislike(key, until=None, now=1.0)
    store.rec_model.record_dislike("other|artist", until=None, now=2.0)
    client = _client(store)
    buttons = UnbanButtons()
    buttons.feed(client.get("/taste/panels/feedback").text)
    assert {"key": key} in buttons.values
    response = client.post("/taste/unban", data={"key": key})
    assert response.status_code == 200 and response.headers["HX-Refresh"] == "true"
    assert store.rec_model.disliked_identity_keys() == {"other|artist"}


def test_feedback_summary_explains_signals_and_excludes_learning_markers(store):
    store.rec_model.record_like("liked|artist", now=1.0)
    store.rec_model.mark_graduated_once("liked|artist", "like_grad", now=1.0)
    store.rec_model.record_feedback("for_you", "later|artist", "not_now", until=100.0, now=1.0)
    store.rec_model.record_feedback("for_you", "artist:Band", "mute", now=1.0)
    html = _client(store).get("/taste/panels/feedback").text
    assert "Liked tracks" in html and "help the model learn what you enjoy" in html
    assert "Snoozed suggestions" in html and "when the snooze expires" in html
    assert "Muted artists or items" in html
    assert "like_grad" not in html and "not_now" not in html
    assert "Your trained model and saved taste weights are kept" in html
    assert "YouTube ratings stay as they are" in html


@pytest.mark.parametrize('clock', ['normal', 'frozen', 'backwards'])
def test_unban_survives_retained_ratings_sync_and_restart_then_accepts_new_dislike(tmp_path, clock):
    path = tmp_path / 'unban.db'
    store = Store(path)
    store.init_schema()
    main = store.identities.upsert_identity('main', 'cred', None, True)
    other = store.identities.upsert_identity('Other', 'cred', 'other', False)
    store.tracks.upsert_track('banned', 'Banned', 'Artist', '', None)
    store.tracks.upsert_track('unrelated', 'Unrelated', 'Artist', '', None)
    key = store.tracks.identity_key_for_video('banned')
    ratings.record_confirmed(store, main, 'banned', 'DISLIKE', 100)
    ratings.record_confirmed(store, other, 'banned', 'DISLIKE', 110 if clock != 'backwards' else 250)
    originals = {iid: store.tracks.item_rating(iid, 'banned') for iid in (main, other)}
    assert key in store.rec_model.suppressed_keys('for_you', 200)
    client = _client(store, now=200)
    assert client.post('/taste/unban', data={'key': key}).status_code == 200
    assert key not in store.rec_model.suppressed_keys('for_you', 200)
    ratings.record_confirmed(store, main, 'unrelated', 'LIKE', 210)
    for iid in (main, other):
        assert store.tracks.pending_action_ratings(iid) == []
        ratings.reconcile_pending(store, iid, None)
        ratings.record_observations(store, iid, {'banned': 'DISLIKE'}, source='sync', now=220)
        assert store.tracks.item_rating(iid, 'banned') == originals[iid]
    # Passive player readouts likewise cannot replace a deliberate local allowance.
    ratings.record_observations(store, other, {'banned': 'DISLIKE'}, source='action', now=230)
    assert key not in store.rec_model.disliked_identity_keys()
    store.conn.close()
    store = Store(path)
    try:
        store.init_schema()
        ratings.reconcile_pending(store, main, None)
        ratings.reconcile_pending(store, other, None)
        ratings.record_observations(store, other, {'banned': 'DISLIKE'}, source='sync', now=240)
        assert key not in store.rec_model.suppressed_keys('for_you', 240)
        now = 300 if clock == 'normal' else 200
        ratings.record_confirmed(store, main, 'banned', 'DISLIKE', now)
        ban, = store.rec_model.list_dislikes()
        assert ban['item_key'] == key
        assert ban['created_at'] >= now and ban['created_at'] > 200
        assert ban['until'] > ban['created_at']
        assert store.tracks.item_rating(main, 'banned')['observed_at'] == ban['created_at']
        ratings.record_observations(store, other, {'banned': 'DISLIKE'}, source='sync', now=400)
        assert store.rec_model.list_dislikes()[0]['created_at'] == ban['created_at']
    finally:
        store.conn.close()


