from contextlib import ExitStack, nullcontext
import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_routes_only, publish_menu, without_metadata_providers
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles, seed_bundle_subjects
from tests.test_home_other_genres import (
    genre_bundles, genre_http_inputs, genre_routes,
    template_bytecode,
)
from tests.test_mode_cards_route import prepare_home
from yt_playlist.rec import graduation, home_cards, mix_tuning, mode_surfaces, rec_params, scoring
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.usefixtures('unlocked_home_controls', 'genre_routes', 'genre_http_inputs')



@pytest.fixture
def store(file_backed_store):
    return file_backed_store


def prepare_tuning_home(store, now):
    store.accounts.upsert_account("main", "cred", None, True)
    prepare_home(store, now)


def client_at(store, clock):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: clock[0]),
                      base_url="http://127.0.0.1")


@pytest.fixture
def home_clients(store, monkeypatch):
    with ExitStack() as resources:
        reader = None
        if store._db_path is not None:
            reader = store._make_reader()
            resources.callback(reader.conn.close)

        def open_client(clock):
            # Route controls consume synchronous reads; connection lifetime
            # has separate owners in core/test_reporting_reads. Worker
            # read_snapshot and its isolation remain real.
            # Memory readers need a current SQL snapshot before binding; their
            # normal read_view owns that copy. A bare _make_reader is empty.
            if reader is not None:
                monkeypatch.setattr(store, 'read_view', lambda **kwargs: nullcontext(reader))
            client = client_at(store, clock)
            # TestClient's context closes the app lifespan; close the HTTP
            # client too, including if startup or the test fails.
            resources.callback(client.close)
            return resources.enter_context(client)

        yield open_client


@pytest.fixture(scope='module')
def tuning_template(_pristine_current_schema):
    def build(source):
        # Expiry consumes completed output; the reset case below retains real
        # genre-filtered selection, and preparation has its own worker owners.
        source.accounts.upsert_account('main', 'cred', None, True)
        publish_menu(source, 1000.0, genre_options=[
            {'family': 'jazz', 'label': 'Jazz'}, {'family': 'techno', 'label': 'Techno'}])

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def prepared_tuning(store, tuning_template):
    tuning_template.restore(store)


def assert_cleared(store):
    assert store.rec_model.get_leans() == {}
    assert store.rec_model.hidden_facets() == set()
    assert rec_params.get_param(store, "breadth_bias") == 0
    assert not store.rec_proposals.get_proposals("home_genre_request")
    assert store.settings.get_setting("home_mix_vibe") is None
    assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) is None
    assert store.settings.get_setting(mix_tuning.VIBE_UPDATED_AT_SETTING) is None


def test_clear_resets_genre_selections_and_all_biases_together(
        store, monkeypatch, home_routes_only, without_metadata_providers):
    clock = [1000.0]
    # Reset owns clearing the combined persisted state and rendering a usable
    # board. Each individual HTTP adjustment is covered by the parametrized
    # four-hour control test below; replaying all six is unrelated setup here.
    # Distinct themes must still fill all four slots after reset.
    bundles = genre_bundles(store, genres=['Jazz', 'Techno', 'Trance', 'Ambient'], tracks_per_lane=4)
    # Each of the four themes can fill its own slot. Reset does not need the
    # sixteen alternative mode/lane pools owned by the assignment tests.
    bundles['all'] = {}
    for mode, lane in enumerate(mode_surfaces.CARD_SURFACES):
        tracks = bundles[str(mode)][lane]
        bundles[str(mode)] = {lane: tracks}
        bundles['all'][lane] = tracks
    seed_bundle_subjects(store, bundles)
    publish_bundles(store, bundles, 1.0)
    monkeypatch.setattr(home_cards.HomeCards, 'audit', lambda *args, **kwargs: None)
    client = client_at(store, clock)
    store.rec_model.set_weight("genre:jazz", 1.4, now=clock[0])
    learned = store.rec_model.get_weights(now=clock[0])
    with mix_tuning.adjusting(store, clock[0]):
        store.rec_proposals.put_proposals('home_genre_request', {
            'epoch': 0, 'include_genres': ['jazz', 'techno']}, clock[0])
        store.rec_model.set_lean('genre:jazz', 2 / 1.4, clock[0])
        store.rec_model.set_lean('genre:gqom', 1, clock[0])
        store.rec_model.hide_facet('genre:techno')
        rec_params.set_param(store, 'breadth_bias', .8)
    with mix_tuning.adjusting_vibe(store, clock[0]):
        store.settings.set_setting('home_mix_vibe', 'relax')
    assert store.rec_proposals.get_proposals("home_genre_request")["include_genres"] == ["jazz", "techno"]
    assert set(store.rec_model.get_leans()) == {"genre:jazz", "genre:gqom"}
    assert store.rec_model.hidden_facets() == {"genre:techno"}
    assert rec_params.get_param(store, "breadth_bias") == .8
    assert store.settings.get_setting("home_mix_vibe") == "relax"
    assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) == "1000.0"
    assert store.settings.get_setting(mix_tuning.VIBE_UPDATED_AT_SETTING) == "1000.0"

    with client:
        response = client.post("/home/fingerprint/reset")
    assert response.status_code == 200
    assert_cleared(store)
    assert store.rec_model.get_weights(now=clock[0]) == learned
    assert 'data-requested-genres=\'[]\'' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text  # mixes and controls update in one response
    assert "Reset mix adjustments" in response.text
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == 0
    cards = re.split(r'<section class="gen-card card"[^>]*>', response.text)[1:]
    assert len(cards) == 4
    assert all(card.count('class="gen-row"') >= 4 for card in cards)
    assert all('aria-label="Save and play this mix"' in card for card in cards)


@pytest.mark.parametrize("path,data", [
    ("/home/vibe", {"vibe": "relax"}),
    ("/home/mix-genres", {"genres": "jazz"}),
    ("/home/other-genres", {"genres": '["Jazz"]'}),
    ("/home/breadth", {"breadth_bias": ".8"}),
    ("/home/steer", {"axis": "genre:jazz", "weight": "2"}),
    ("/home/fingerprint/add", {"axis": "genre:gqom"}),
    ("/home/fingerprint/remove", {"axis": "genre:jazz"}),
])
def test_every_control_expires_at_four_hours_without_reads_extending_it(
        store, path, data, prepared_tuning, home_clients):
    clock = [1000.0]
    client = home_clients(clock)
    response = client.post(path, data=data)
    assert response.status_code == 200
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == mix_tuning.TTL_S

    clock[0] += mix_tuning.TTL_S - 1
    response = client.get("/home/cards")
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == 1
    deadline = mix_tuning.VIBE_UPDATED_AT_SETTING if path == '/home/vibe' else mix_tuning.UPDATED_AT_SETTING
    assert store.settings.get_setting(deadline) == "1000.0"
    # A new client still sees the persisted expiry, rather than starting a new browser timer.
    clock[0] += 1
    reopened = home_clients(clock)
    assert reopened is not client
    assert store.settings.get_setting(deadline) == "1000.0"
    response = reopened.get("/home/fingerprint")
    assert response.status_code == 200
    assert_cleared(store)
    assert 'data-requested-genres=\'[]\'' in response.text
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == 0


def test_last_adjustment_extends_window_and_new_session_drops_expired_choices(
        store, prepared_tuning, home_clients):
    clock = [1000.0]
    client = home_clients(clock)
    client.post("/home/mix-genres", data={"genres": "jazz"})
    clock[0] += 3600
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "2"})
    clock[0] = 1000 + mix_tuning.TTL_S
    client.get("/home/cards")
    assert store.rec_model.get_lean("genre:jazz") == 2
    assert store.rec_proposals.get_proposals("home_genre_request")["include_genres"] == ["jazz"]

    clock[0] += 3600
    client.post("/home/breadth", data={"breadth_bias": ".5"})
    assert store.rec_model.get_leans() == {}
    assert store.rec_proposals.get_proposals("home_genre_request") == {}
    assert rec_params.get_param(store, "breadth_bias") == .5
    assert mix_tuning.remaining(store, clock[0]) == mix_tuning.TTL_S


def test_vibe_expiry_restores_still_active_genre_controls_without_resetting_them(
        store, prepared_tuning, home_clients):
    from yt_playlist.rec import home_vibe
    clock = [1000.0]
    client = home_clients(clock)
    client.post('/home/vibe', data={'vibe': 'relax'})
    clock[0] += 3600
    client.post('/home/mix-genres', data={'genres': 'jazz'})
    client.post('/home/steer', data={'axis': 'genre:techno', 'weight': '2'})
    clock[0] = 1000 + mix_tuning.TTL_S
    client.get('/home/fingerprint')
    assert not home_vibe.current(store, clock[0])
    assert store.rec_model.get_lean('genre:techno') == 2
    assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['jazz']
    assert mix_tuning.remaining(store, clock[0]) == 3600


def test_ordinary_expiry_keeps_a_newer_vibe_and_clearing_vibe_keeps_newer_controls(store, home_clients):
    clock = [1000.0]
    prepare_tuning_home(store, clock[0])
    client = home_clients(clock)
    client.post('/home/steer', data={'axis': 'genre:jazz', 'weight': '2'})
    clock[0] += 3600
    client.post('/home/vibe', data={'vibe': 'cruise'})
    clock[0] = 1000 + mix_tuning.TTL_S
    client.get('/home/cards')
    assert not store.rec_model.get_leans()
    assert store.settings.get_setting('home_mix_vibe') == 'cruise'
    assert mix_tuning.remaining(store, clock[0]) == 3600
    client.post('/home/breadth', data={'breadth_bias': '.8'})
    deadline = store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING)
    client.post('/home/vibe', data={'vibe': ''})
    assert rec_params.get_param(store, 'breadth_bias') == .8
    assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) == deadline
    assert mix_tuning.remaining(store, clock[0]) == mix_tuning.TTL_S


@pytest.mark.parametrize("consumer", ["cards", "scoring", "discovery", "graduation"])
def test_expired_biases_stop_affecting_recommendations_without_opening_controls(store, consumer, home_clients):
    clock = [1000.0]
    prepare_tuning_home(store, clock[0])
    client = home_clients(clock)
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "2"})
    client.post("/home/breadth", data={"breadth_bias": ".5"})
    clock[0] += mix_tuning.TTL_S
    if consumer == "cards":
        client.get("/home/cards")
    elif consumer == "scoring":
        assert scoring._axis_weights_for(store, [], now=clock[0]) is None
    elif consumer == "discovery":
        assert scoring.discovery_facet_weight(store, "jazz", clock[0]) == 1
    else:
        graduation.graduate_slider_exposure(store, clock[0])
    assert_cleared(store)
    assert store.rec_model.get_weights(now=clock[0]) == {}


def test_temporary_bias_does_not_become_learned_taste_across_midnight(store, home_clients):
    clock = [86400.0 - 60]
    prepare_tuning_home(store, clock[0])
    client = home_clients(clock)
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "2"})
    graduation.graduate_slider_exposure(store, clock[0])
    clock[0] += 120
    graduation.graduate_slider_exposure(store, clock[0])
    assert store.rec_signals.theme_rows() == []
    assert store.rec_model.get_weights(now=clock[0]) == {}
