"""Taste overview, topic navigation and listening readings."""
from tests.derived_rating_support import seed_dislike, seed_like
import pytest
from fastapi.testclient import TestClient

from yt_playlist.core import radio_provenance
from yt_playlist.library.listening_composition import layer_plays
from yt_playlist.rec import embedding_vectors as embed, rec_params, taste_viz
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, seed_catalog_track
from tests.group_support import publish_keyed
from tests.taste_support import _client
from tests.store_fixture_support import bounded_consumer_schema


@pytest.fixture(scope='session')
def _pristine_current_schema():
    # Page consumers use real SQL and guards with the genres present in their
    # complete datasets. Catalog curation and schema admission have their own tests.
    with bounded_consumer_schema((('jazz', 'Jazz'), ('techno', 'Techno'))) as schema:
        yield schema


@pytest.fixture(autouse=True)
def taste_routes(monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_vibes, taste

    # Keep the actual Taste handlers and shared template dependencies. Building
    # unrelated page routers is not part of these requests.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (app_update, audio_vibes, taste)])


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
    ("radio", "Today’s plays appear in listening history."),
    ("dated", "precise times aren’t available for this comparison."),
    ("unmapped", "Today’s listening needs more comparable music to show a balance."),
    ("quiet", "No listening recorded today."),
])
def test_overview_shows_weekly_model_inputs_when_the_play_comparison_is_unavailable(store, source, explanation):
    now = 20 * 86400.0
    client = _client(store, now=now)
    iid = store.accounts.get_accounts()[0].id
    keys = [f'jazz {i}|artist' for i in range(4)]
    for key in keys:
        seed_catalog_track(store, key, key, 'Jazz Artist', genre='Jazz')
    publish_keyed(store, [('Jazz', keys, .5)], now=now)
    with store._lock, store.conn:
        radio_provenance.remember(store.conn, 'radio', iid)
    if source in ("radio", "unmapped"):
        store.history.record_play_event(iid, "song|artist", "v1", now,
                                        playlist_ytm_id="radio" if source == "radio" else None,
                                        source_timezone="UTC")
    elif source == "dated":
        store.history.record_history_plays(iid, now, ["song|artist"], source_timezone="UTC")
    elif source == "quiet":
        store.history.record_play_event(iid, "song|artist", "v1", now - 3600, source_timezone="UTC")
    seed_like(store, "liked|artist", now - 6 * 86400, provenance="action")
    seed_like(store, "synced|artist", now, provenance="sync")
    seed_dislike(store, "disliked|artist", now + 86400, now - 2 * 86400)
    store.rec_signals.log_graduation("era:1990", "like", 2, 1.1, 1.1, now - 5 * 86400)

    html = client.get("/taste/panels/overview").text

    assert explanation in html
    assert 'Listening balance <span>Today</span>' in html
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
    seed_like(store, "liked|artist", 1, provenance="action")

    html = _client(store).get("/taste/panels/overview").text

    assert "Like recorded<small>From your ratings</small>" in html
    assert "This comparison appears once your library has been grouped" in html
    assert "matched tracks" not in html


def test_genre_coverage_counts_tracks_instead_of_weighted_plays(store):
    c = _client(store)
    iid = store.accounts.get_accounts()[0].id
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


def test_taste_page_shows_controls_for_current_listener_policy(store):
    html = _client(store).get("/taste/panels/learning").text
    assert 'class="taste-settings-section taste-settings-layout"' in html
    for label in ("Recent influence", "Listening memory", "Current session"):
        assert f'aria-label="{label}"' in html
    assert '<details' not in html
    # Each setting has one home, including the formerly nested advanced controls.
    for param in rec_params.PARAMS:
        expected = int(param.group == "transient" and param.name not in rec_params.SEMANTIC_PARAMS
                       and param.name not in ('now_window_h', 'weight_revert_halflife_d'))
        assert html.count(f'id="param-{param.name}"') == expected, param.name
    assert 'id="param-graduation_enabled"' not in html
    assert "Learning paused" not in html
    assert "Learning enabled" not in html
    assert 'id="param-play_transient_w"' in _client(store).get("/taste/panels/recommendations").text


def test_taste_page_has_one_bar_of_section_tabs(store):
    iid = store.accounts.upsert_account("main", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    html = c.get("/taste").text
    assert 'aria-label="Taste Model sections"' in html
    page = html.split('<main id="app-page"', 1)[1].split('</main>', 1)[0]
    assert page.count('role="tablist"') == 1
    assert html.index('id="taste-overview-tab"') < html.index('id="taste-experiment-tab"') < html.index('id="taste-profile-tab"')
    assert "Recommendations" in html
    assert "Model health" in html and "Feedback" in html
    assert "Recent activity" not in html
    html = c.get("/taste/panels/learning").text
    assert "Recent activity" in html
    assert "Learning progress" in html
    assert "Influence fades with time" in html
    assert "The last available readings stay visible while dated listening evidence prepares." in html
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
        assert "TODAY resets at local midnight" in sound.text
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


@pytest.mark.parametrize('playlist', [None, 'radio'])
def test_taste_page_shows_now_reading_from_published_groups(store, playlist):
    keys = [f'techno {i}|artist' for i in range(12)]
    for i, key in enumerate(keys):
        seed_catalog_track(store, key, f'Techno {i}', 'Artist', video_id=f'techno-{i}', genre='Techno')
    publish_keyed(store, [('Techno', keys, .9)])
    iid = store.accounts.upsert_account("main", None, True)
    now = 100_000.0
    if playlist:
        with store._lock, store.conn:
            radio_provenance.remember(store.conn, playlist, iid)
    for i in (2, 1, 0):
        store.history.record_play_event(iid, keys[i], f'techno-{i}', now - i * 60,
                                        playlist_ytm_id=playlist, source_timezone='UTC')
    assert store.history.import_play_events(iid, [(keys[3], 'techno-3', now - 30)]) == 1

    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                   base_url="http://127.0.0.1")
    html = c.get("/taste/panels/profile").text
    assert "Listening styles" in html
    assert "Techno" in html
    assert "active session · 3/3 plays matched" in html
    assert "since local midnight · 4/4 plays matched" in html
    assert "100%" in html               # single-mode ribbon segment share, in the tooltip
    assert set(layer_plays(store, now).current) == (set() if playlist else set(keys[:3]))
    assert set(layer_plays(store, now).today) == ({keys[3]} if playlist else set(keys[:4]))


def test_taste_page_hides_now_and_session_reading_when_quiet(store):
    # No modes, no recent plays -> below the confidence gate. The card stays, the fast rows go quiet.
    html = _client(store).get("/taste/panels/profile").text
    assert "Listening styles" in html
    assert "No listening styles found yet" in html


def test_taste_page_draws_the_full_chain_on_a_facet_axis(store):
    # #88: wherever the page contrasts permanent and transient it must draw every layer that exists
    # on that axis - here, the whole multiplier chain, not just a share and a shrug.
    iid = store.accounts.upsert_account("main", None, True)
    t = store.tracks.upsert_track("vj", "Song", "Band", None, None)
    store.tracks.set_track_genre(t, "Jazz")
    store.history.record_history_plays(iid, 1.0, ["song|band"], source_timezone="UTC")
    html = _client(store).get("/taste/panels/profile").text
    assert "Genre families" in html
    for cap in ("What you play", "Saved", "Recent", "Combined"):
        assert cap in html
    assert "Recent multiplier" in html
    assert "= Combined multiplier" in html
