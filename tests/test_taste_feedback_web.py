"""Taste feedback summaries, track bans and unban routes."""
import json
from html.parser import HTMLParser
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


