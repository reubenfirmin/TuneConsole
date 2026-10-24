"""Taste overview, topic navigation and listening readings."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import embedding_vectors as embed, rec_params, taste_viz
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.taste_support import _client


def test_taste_page_renders_status_and_controls(store):
    c = _client(store)
    html = c.get("/taste").text
    assert "Taste Model" in html
    assert 'name="weight"' not in html
    assert 'name="weight"' in c.get("/taste/panels/generated").text
    assert "/taste/autotune" in c.get("/taste/panels/health").text


def test_taste_shell_and_overview_do_not_compute_hidden_tabs(store, monkeypatch):
    c = _client(store)

    def unexpected(*args, **kwargs):
        raise AssertionError("Hidden tab data was loaded")

    monkeypatch.setattr(store.rec_evidence, "rec_baskets", unexpected)
    monkeypatch.setattr(taste_viz, "model_transparency", unexpected)
    monkeypatch.setattr(taste_viz, "generated_playlists", unexpected)
    monkeypatch.setattr(embed, "load_vectors", unexpected)
    assert c.get("/taste").status_code == 200
    response = c.get("/taste/panels/overview")
    assert response.status_code == 200
    assert "Your long-term taste" in response.text
    assert "What’s changed recently?" in response.text
    assert 'type="range"' not in response.text
    experiment = c.get("/taste/panels/experiment")
    assert experiment.status_code == 200
    assert "Your starting point" in experiment.text
    assert "Preview &amp; compare" in experiment.text
    assert "settings across Taste Model" in experiment.text
    assert "/taste/checkpoint/restore" in experiment.text
    assert "/taste/reset-all" in experiment.text
    assert 'aria-label="Experiment tools"' in experiment.text
    for topic in ("starting", "preview"):
        assert f'id="taste-experiment-{topic}-tab"' in experiment.text
        assert f'id="taste-experiment-{topic}-panel"' in experiment.text
    assert c.get("/taste/panels/unknown").status_code == 404


@pytest.mark.parametrize("source,explanation", [
    ("radio", "Recent radio listening is recorded."),
    ("dated", "precise times aren’t available for this comparison."),
    ("unmapped", "Recent listening is recorded, but there isn’t enough comparable music yet"),
    ("quiet", "A recent listening comparison isn’t available yet."),
])
def test_overview_shows_weekly_model_inputs_when_the_play_comparison_is_unavailable(store, source, explanation):
    now = 20 * 86400.0
    client = _client(store, now=now)
    iid = store.identities.get_identities()[0].id
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Jazz", "families": [["jazz", 1]],
         "centroid": np.ones(1, dtype=np.float32), "size": 3, "rep_keys": []},
    ], retired_ids=[], now=now)
    store.settings.set_setting("radio_session_playlist_ids", '["radio"]')
    if source in ("radio", "unmapped"):
        store.history.record_play_event(iid, "song|artist", "v1", now,
                                        playlist_ytm_id="radio" if source == "radio" else None,
                                        source_timezone="UTC")
    elif source == "dated":
        store.history.record_history_plays(iid, now, ["song|artist"], source_timezone="UTC")
    store.rec_model.record_like("liked|artist", now - 6 * 86400, provenance="action")
    store.rec_model.record_like("synced|artist", now, provenance="sync")
    store.rec_model.record_dislike("disliked|artist", now + 86400, now - 2 * 86400)
    store.rec_signals.log_graduation("era:1990", "like", 2, 1.1, 1.1, now - 5 * 86400)

    html = client.get("/taste/panels/overview").text

    assert explanation in html
    assert "last 7 days" in html
    assert "Like recorded<small>From your ratings</small>" in html
    assert "Existing like<small>Found by sync</small>" in html
    assert "their original rating dates aren’t known." in html
    assert "Dislike added</dt><dd>1</dd>" in html
    assert "<strong>1990s</strong>" in html and '>Increased</span>' in html
    assert 'class="taste-preference-axis">Era</span>' in html
    assert html.index('Saved preference changes') < html.index('Feedback added to your model') < html.index('Listening balance')
    assert '<details class="taste-feedback-note">' in html
    assert "Ratings on radio songs count too." in html
    assert "matched tracks" not in html
    assert "At least 3" not in html
    assert 'href="#learning"' in html


def test_overview_shows_feedback_even_without_a_content_model_or_music_groups(store):
    store.rec_model.record_like("liked|artist", 1, provenance="action")

    html = _client(store).get("/taste/panels/overview").text

    assert "Like recorded<small>From your ratings</small>" in html
    assert "This comparison appears once your library has been grouped" in html
    assert "matched tracks" not in html


def test_genre_coverage_counts_tracks_instead_of_weighted_plays(store):
    c = _client(store)
    iid = store.identities.get_identities()[0].id
    tagged = store.tracks.upsert_track("tagged", "Tagged", "Artist", None, None)
    store.tracks.set_track_genre(tagged, "Jazz")
    store.tracks.upsert_track("untagged", "Untagged", "Artist", None, None)
    for day in range(4):
        store.history.record_history_plays(iid, day * 86400.0, ["tagged|artist"], source_timezone="UTC")

    html = c.get("/taste/model-status").text

    assert "Genre coverage" in html
    assert "1 of 2 library tracks have a genre tag" in html
    assert "50%" in html


def test_model_selection_is_automatic_and_old_manual_route_cannot_start_a_sweep(store):
    c = _client(store)
    r = c.post("/taste/autotune")
    assert r.status_code == 409
    assert "automatically" in r.text
    assert "HX-Refresh" not in r.headers
    # Status endpoint is reachable and renders the fragment.
    s = c.get("/taste/autotune-status")
    assert s.status_code == 200


def test_taste_page_shows_transient_and_graduation_controls(store):
    html = _client(store).get("/taste/panels/learning").text
    assert 'class="taste-settings-section taste-settings-layout"' in html
    for label in ("Recent influence", "Listening memory", "Current session", "Saved preferences", "Learning rates"):
        assert f'aria-label="{label}"' in html
    assert '<details' not in html
    # Each setting has one home, including the formerly nested advanced controls.
    for param in rec_params.PARAMS:
        expected = int(param.group in ("transient", "graduation")
                       and param.name not in rec_params.SEMANTIC_PARAMS)
        assert html.count(f'id="param-{param.name}"') == expected, param.name
    assert 'id="param-graduation_enabled"' not in html
    assert "Learning paused" not in html
    assert "Learning enabled" not in html
    assert 'id="param-play_transient_w"' in _client(store).get("/taste/panels/recommendations").text


def test_taste_page_has_one_bar_of_section_tabs(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste").text
    assert 'aria-label="Taste Model sections"' in html
    assert html.count('role="tablist"') == 1
    assert html.index('id="taste-overview-tab"') < html.index('id="taste-experiment-tab"') < html.index('id="taste-profile-tab"')
    assert "Recommendations" in html
    assert "Model health" in html and "Feedback" in html
    assert "Recent activity" not in html
    html = c.get("/taste/panels/learning").text
    assert "Recent activity" in html
    assert "Learning progress" in html
    assert "how quickly recent influence fades" in html
    assert "At 100%, its saved preference gets a small update" in html
    assert "The four layers" not in html


def test_learning_tab_does_not_load_style_or_similarity_models(store, monkeypatch):
    from yt_playlist.rec import taste_viz

    def unexpected(*args, **kwargs):
        raise AssertionError("Learning must not load the other tabs' models")

    monkeypatch.setattr(taste_viz, "model_transparency", unexpected)
    monkeypatch.setattr(embed, "load_content_vectors", unexpected)
    monkeypatch.setattr(embed, "load_vectors", unexpected)
    store.rec_signals.bump_theme("artist:AC:DC", .6, 1.0)
    response = _client(store).get("/taste/panels/learning")
    assert response.status_code == 200
    assert "AC:DC" in response.text
    assert response.text.count('class="learning-card learning-card--') == 6


def test_model_health_kpis_render_without_loading_listening_models(store, monkeypatch):
    from yt_playlist.rec import taste_viz

    def unexpected(*args, **kwargs):
        raise AssertionError("Model KPIs must not load listening models")

    monkeypatch.setattr(taste_viz, "centroid_tilt_panel", unexpected)
    monkeypatch.setattr(taste_viz, "playlist_influence_panel", unexpected)
    monkeypatch.setattr(embed, "load_vectors", unexpected)
    store.settings.set_setting("rec_embed_method", "item2vec")
    store.settings.set_setting("rec_dim", "64")
    c = _client(store)
    for path in ("/taste/panels/health", "/taste/model-status"):
        r = c.get(path)
        assert r.status_code == 200
        assert "Tracks modeled" in r.text
        assert "Learning method" in r.text and "Item2Vec" in r.text
        assert "64<small> dimensions</small>" in r.text
        assert "model-health-details" not in r.text
        assert "Recent sound" not in r.text
        assert "Playlist influence" not in r.text


def test_taste_listening_readings_have_separate_destinations(store, monkeypatch):
    from yt_playlist.rec import taste_viz

    def unexpected(*args, **kwargs):
        raise AssertionError("This reading must not load the other reading's model")

    client = _client(store)
    with monkeypatch.context() as m:
        m.setattr(taste_viz, "playlist_influence_panel", unexpected)
        sound = client.get("/taste/viz/recent-sound")
        assert sound.status_code == 200
        assert "Not enough recent listening to compare sounds yet" in sound.text
    with monkeypatch.context() as m:
        m.setattr(taste_viz, "centroid_tilt_panel", unexpected)
        influence = client.get("/taste/viz/playlist-influence")
        assert influence.status_code == 200
        assert "saved records, playlists and listening choices" in influence.text

    profile = client.get("/taste/panels/profile").text
    learning = client.get("/taste/panels/learning").text
    health = client.get("/taste/panels/health").text
    assert 'hx-get="/taste/viz/playlist-influence"' in profile
    assert 'hx-get="/taste/viz/recent-sound"' not in profile
    assert 'hx-get="/taste/viz/recent-sound"' in learning
    assert 'hx-get="/taste/viz/playlist-influence"' not in learning
    assert "Learning method" in health and "Model size" in health
    assert "Recent sound" not in health and "Playlist influence" not in health
    assert "Track similarity model</h2>" not in health


def test_taste_page_shows_now_reading_with_live_posterior(store, monkeypatch):
    # #88: a live NOW posterior (seeded the same way test_now_layer.py does) renders as the NOW
    # ribbon on the Taste modes card - the only card on which the NOW layer exists.
    store.modes.replace_modes([
        {"mode_id": 1, "label": "Warehouse techno", "families": [["techno", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 50, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    keys = ["a1", "a2", "a3"]
    V = np.array([[1.0, 0.02], [1.0, 0.03], [1.0, 0.01]], dtype=np.float32)
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    idx = {k: i for i, k in enumerate(keys)}
    monkeypatch.setattr(embed, "load_content_vectors", lambda s, **kw: (keys, V, idx))
    iid = store.identities.upsert_identity("main", "cred", None, True)
    now = 100_000.0
    rows = [(k, "v" + k, now - i * 60) for i, k in enumerate(keys)]
    store.history.import_play_events(iid, rows)

    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                   base_url="http://127.0.0.1")
    html = c.get("/taste/panels/profile").text
    assert "Listening styles" in html
    assert "Warehouse techno" in html   # NOW/SESSION ribbon tooltip title
    assert "last 6h" in html            # NOW row's timescale (now_window_h default)
    assert "24h" in html and "influence halves every 4h" in html
    assert "100%" in html               # single-mode ribbon segment share, in the tooltip


def test_taste_page_hides_now_and_session_reading_when_quiet(store):
    # No modes, no recent plays -> below the confidence gate. The card stays, the fast rows go quiet.
    html = _client(store).get("/taste/panels/profile").text
    assert "Listening styles" in html
    assert "No listening styles found yet" in html


def test_taste_page_draws_the_full_chain_on_a_facet_axis(store):
    # #88: wherever the page contrasts permanent and transient it must draw every layer that exists
    # on that axis - here, the whole multiplier chain, not just a share and a shrug.
    iid = store.identities.upsert_identity("main", "cred", None, True)
    t = store.tracks.upsert_track("vj", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Jazz")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    html = _client(store).get("/taste/panels/profile").text
    assert "Genre families" in html
    for cap in ("What you play", "Saved", "Recent", "Combined"):
        assert cap in html
    assert "Recent multiplier" in html
    assert "= Combined multiplier" in html
