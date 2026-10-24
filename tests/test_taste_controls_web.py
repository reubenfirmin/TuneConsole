"""Taste control rendering, parameter persistence and recovery checkpoints."""
import json
import numpy as np
import pytest
from html.parser import HTMLParser
from fastapi.testclient import TestClient

from yt_playlist.rec import rec_params
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.taste_support import _client

def test_retired_source_weight_cannot_be_saved_or_learned(store):
    client = _client(store)
    response = client.post("/taste/weight", data={"axis": "lane:explore", "weight": "1.3"})
    assert response.status_code == 400
    assert store.settings.get_setting("taste_settings_checkpoint_v1") is None
    for kind in ("more", "less", "dismiss", "not_now"):
        response = client.post("/recs/feedback", data={"item": "old catalog track|artist",
                                                     "kind": kind, "lane": "explore"})
        assert response.status_code == 200
    assert "lane:explore" not in store.rec_model.weight_records()


def test_preference_binding_knob_is_on_the_page_and_round_trips(store):
    """The axis_weight_cap knob decides whether a stated genre preference tilts the ranking or
    replaces it, so it remains reachable and reversible in diagnostic controls."""
    from yt_playlist.rec import rec_params
    from yt_playlist.rec.scoring import AXIS_WEIGHT_UNBOUNDED
    c = _client(store)

    html = c.get("/taste/panels/recommendations").text
    assert 'aria-label="Ranking &amp; feedback"' in html
    assert "Fine-tune your preferences" not in html
    assert "Preference strength" in html
    assert 'id="param-axis_weight_cap"' in html
    assert rec_params.get_param(store, "axis_weight_cap") == 1.4

    assert c.post("/taste/param", data={"name": "axis_weight_cap", "value": "1.4"}).status_code == 200
    assert rec_params.get_param(store, "axis_weight_cap") == 1.4

    assert c.post("/taste/reset-param", data={"name": "axis_weight_cap"}).status_code == 200
    assert rec_params.get_param(store, "axis_weight_cap") == 1.4


def test_taste_page_separates_semantic_controls_from_topic_settings(store):
    html = _client(store).get("/taste/panels/recommendations").text
    assert "Main controls" in html
    assert html.count('role="tablist"') == 2
    assert 'aria-label="Recommendation settings"' in html
    assert 'aria-label="Features"' in html
    for topic in ("intro", "main", "genres", "features"):
        assert f'id="taste-recommendation-{topic}-tab"' in html
        assert f'id="taste-recommendation-{topic}-panel"' in html
    assert "Home and Discover" in html and 'href="#experiment"' in html
    assert 'id="taste-checkpoint"' not in html
    assert 'id="taste-preview"' not in html
    assert "/taste/reset-all" not in html
    for name in rec_params.SEMANTIC_PARAMS:
        assert html.count(f'id="param-{name}"') == 1


def test_maximum_track_length_setting_controls_generated_playlists(store):
    from yt_playlist.library.playlist_generated import create_generated_playlist

    client = _client(store)
    html = client.get("/taste/panels/recommendations").text
    panel = html.split('id="taste-feature-home"', 1)[1].split('</section>', 1)[0]
    assert 'id="param-max_track_minutes"' in panel
    assert "Maximum track length (minutes)" in panel
    assert 'Default 20' in panel
    assert client.post("/taste/param", data={"name": "max_track_minutes", "value": "120"}).status_code == 200

    remote = FakeClient(song_durations={"concert": 7200})
    result = create_generated_playlist(store, "Gen", [{"video_id": "concert"}], remote, 1)
    assert result["added"] == 1
    assert client.post("/taste/reset-param", data={"name": "max_track_minutes"}).status_code == 200
    assert rec_params.get_param(store, "max_track_minutes") == 20
    with pytest.raises(ValueError, match="maximum track length"):
        create_generated_playlist(store, "Gen", [{"video_id": "concert"}], remote, 2)
    assert len(remote.created) == 1


def test_taste_page_uses_sliders_not_number_fields(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste/panels/recommendations").text
    assert 'type="range"' in html                 # knobs are sliders now
    assert 'type="number"' not in html            # ...not typed number fields
    assert "/taste/param" in html                 # scalar knobs post here
    assert "Source weights" in c.get("/taste/panels/generated").text
    assert "Settings by feature" in html


def test_taste_page_renders_genre_slider_for_tagged_library(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Techno")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste/panels/recommendations").text
    assert 'genre:techno' in html                 # a per-genre-family weight control is rendered


def test_taste_controls_round_trip(store):
    c = _client(store)
    assert c.post("/taste/weight", data={"axis": "lane:deep_cut", "weight": "0.5"}).status_code == 200
    assert store.rec_model.get_weights(now=1.0)["lane:deep_cut"] == 0.5
    assert c.post("/taste/reset-weights").status_code == 200
    assert store.rec_model.get_weights() == {}
    store.rec_model.record_feedback("for_you", "a|b", "dismiss", now=1.0)
    assert c.post("/taste/clear-feedback").status_code == 200
    assert store.rec_model.suppressed_keys("for_you", now=2.0) == set()


def test_taste_recall_fragment(store):
    c = _client(store)
    r = c.get("/taste/recall")
    assert r.status_code == 200   # no vectors -> "build first", still 200
    assert "1-day test period" in r.text


def test_taste_param_saves_and_marks_sample_stale(store):
    c = _client(store)
    r = c.post("/taste/param", data={"name": "comfort_recency_full_days", "value": "60"})
    assert r.status_code == 200
    assert r.headers.get("hx-refresh") != "true"          # no full reload (the old "flashing" cause)
    assert r.headers.get("hx-trigger") == "taste-stale"   # marks the live sample stale instead
    assert rec_params.get_param(store, "comfort_recency_full_days") == 60


def test_taste_param_is_clamped(store):
    c = _client(store)
    c.post("/taste/param", data={"name": "breadth_bias", "value": "9"})
    assert rec_params.get_param(store, "breadth_bias") == 1.0   # max


def test_retired_genre_familiarity_control_cannot_be_saved_or_restored(store):
    c = _client(store)
    key = 'rec_param:palette_absence_penalty'
    store.settings.set_setting('taste_settings_checkpoint_v1', json.dumps({
        'saved_at': 1.0, 'params': {key: '1.5', 'rec_param:breadth_bias': '-0.5'},
        'weights': [],
    }))
    html = c.get('/taste/panels/recommendations').text
    assert 'Favor familiar genres' not in html
    assert 'palette_absence_penalty' not in html
    assert c.post('/taste/param', data={'name': 'palette_absence_penalty', 'value': '1.5'}).status_code == 200
    assert c.post('/taste/checkpoint/restore').status_code == 200
    assert store.settings.get_setting(key) is None
    assert rec_params.get_param(store, 'breadth_bias') == -0.5
    assert c.post('/taste/reset-all').status_code == 200
    assert rec_params.get_param(store, 'breadth_bias') == 0.0


@pytest.mark.parametrize("name", ["onboard_coverage_min", "onboard_library_min"])
def test_retired_onboarding_control_cannot_be_saved_or_restored(store, name):
    c = _client(store)
    key = rec_params.SETTING_PREFIX + name
    store.settings.set_setting("taste_settings_checkpoint_v1", json.dumps({
        "saved_at": 1.0, "params": {key: "100", "rec_param:onboard_feedback_min": "15"},
        "weights": [],
    }))
    html = c.get("/taste/panels/recommendations").text
    assert name not in html
    assert 'id="param-onboard_feedback_min"' in html
    assert c.post("/taste/param", data={"name": name, "value": "100"}).status_code == 200
    assert c.post("/taste/checkpoint/restore").status_code == 200
    assert store.settings.get_setting(key) is None
    assert rec_params.get_param(store, "onboard_feedback_min") == 15
    assert c.post("/taste/reset-param", data={"name": name}).status_code == 200
    assert store.settings.get_setting(key) is None


def test_taste_genre_weight_uses_genre_band(store):
    c = _client(store)
    # a genre axis may be muted to 0 (band [0,2]); the lane band [0.2,3.0] must not floor it
    assert c.post("/taste/weight", data={"axis": "genre:rock", "weight": "0"}).status_code == 200
    assert store.rec_model.get_weights(now=1.0)["genre:rock"] == 0.0


def test_taste_reset_param_restores_default(store):
    c = _client(store)
    rec_params.set_param(store, "comfort_min_plays", 3)
    assert c.post("/taste/reset-param", data={"name": "comfort_min_plays"}).status_code == 200
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_taste_reset_all_clears_weights_and_params(store):
    c = _client(store)
    store.rec_model.set_weight("lane:deep_cut", 0.5)
    rec_params.set_param(store, "comfort_min_plays", 3)
    assert c.post("/taste/reset-all").status_code == 200
    assert store.rec_model.get_weights() == {}
    assert rec_params.get_param(store, "comfort_min_plays") == 4


def test_recommendation_controls_are_all_available_and_rendered_once(store):
    class RecommendationPanel(HTMLParser):
        def __init__(self):
            super().__init__()
            self.details = []
            self.features = []
            self.ids = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "details":
                self.details.append(attrs)
            if tag == "section" and attrs.get("class") == "taste-feature-section":
                self.features.append(attrs)
            if tag == "input":
                self.ids.append(attrs.get("id"))

    client = _client(store)
    for query in ["", "?radio=false", "?radio=0", "?radio=true", "?radio=1"]:
        panel = RecommendationPanel()
        html = client.get("/taste/panels/recommendations" + query).text
        panel.feed(html)
        assert panel.details == []
        assert len(panel.features) == 8
        assert all(f.get("role") == "tabpanel" and f.get("aria-labelledby") == f'{f["id"]}-tab'
                   for f in panel.features)
        for p in rec_params.PARAMS:
            if p.group == "discovery" or p.name in rec_params.SEMANTIC_PARAMS:
                expected = 0 if p.name.startswith("radio_") else 1
                assert panel.ids.count(f"param-{p.name}") == expected, p.name
        assert "radio" not in html.lower()
        assert 'hx-get="/taste/panels/recommendations"' in client.get("/taste" + query).text


def test_first_change_saves_recovery_point_and_later_changes_keep_it(store):
    client = _client(store)
    store.rec_model.set_weight("genre:rock", 0, lo=0, now=-1000)
    store.rec_model.set_weight("era:1990s", 1.7, now=-500)
    original_weights = store.rec_model.weight_records()
    rec_params.set_param(store, "breadth_bias", -0.25)
    rec_params.set_param(store, "source_w_play", 0.12)
    store.settings.set_setting("unrelated_setting", "keep me")
    assert "automatically saves" in client.get("/taste/checkpoint").text

    client.post("/taste/param", data={"name": "breadth_bias", "value": "0.8"})
    client.post("/taste/weight", data={"axis": "genre:rock", "weight": "1.5"})
    client.post("/taste/weight", data={"axis": "genre:jazz", "weight": "0"})
    client.post("/taste/param", data={"name": "comfort_min_plays", "value": "12"})
    client.post("/taste/param", data={"name": "source_w_play", "value": "0.2"})
    store.rec_model.record_feedback("for_you", "song|artist", "dismiss", now=1)
    store.rec_model.replace_rec_vectors([("song|artist", np.array([1., 0.], dtype=np.float32).tobytes())])

    assert client.post("/taste/checkpoint/restore").headers["HX-Refresh"] == "true"
    assert rec_params.get_param(store, "breadth_bias") == -0.25
    assert rec_params.get_param(store, "source_w_play") == 0.12
    assert store.settings.get_setting("rec_param:comfort_min_plays") is None
    assert store.rec_model.weight_records() == original_weights  # includes zero and original fade timestamps
    assert store.settings.get_setting("unrelated_setting") == "keep me"
    assert store.rec_model.suppressed_keys("for_you", now=1) == {"song|artist"}
    assert store.rec_model.rec_vectors_count() == 1
    # Reopening the page/app does not discard the checkpoint.
    assert "Starting point saved" in _client(store).get("/taste/checkpoint").text


def test_checkpoint_can_be_replaced_and_survives_reset_all(store):
    client = _client(store)
    client.post("/taste/param", data={"name": "breadth_bias", "value": "0.5"})
    assert client.post("/taste/checkpoint").status_code == 200
    client.post("/taste/reset-all")
    assert rec_params.get_param(store, "breadth_bias") == 0
    client.post("/taste/checkpoint/restore")
    assert rec_params.get_param(store, "breadth_bias") == 0.5


def test_reset_all_itself_saves_a_recovery_point(store):
    client = _client(store)
    store.rec_model.set_weight("artist:someone", 1.6, now=None)
    rec_params.set_param(store, "comfort_min_plays", 9)
    client.post("/taste/reset-all")
    client.post("/taste/checkpoint/restore")
    assert rec_params.get_param(store, "comfort_min_plays") == 9
    assert store.rec_model.weight_records()["artist:someone"]["weight"] == 1.6


