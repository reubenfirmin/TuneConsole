import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.test_home_other_genres import seed_genres
from yt_playlist.rec import graduation, mix_tuning, rec_params, scoring
from yt_playlist.web.app import create_app


def client_at(store, clock):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: clock[0]),
                      base_url="http://127.0.0.1")


def assert_cleared(store):
    assert store.rec_model.get_leans() == {}
    assert store.rec_model.hidden_facets() == set()
    assert rec_params.get_param(store, "breadth_bias") == 0
    assert store.rec_surface.get_proposals("home_genre_request") == {}
    assert store.settings.get_setting("home_mix_vibe") is None
    assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) is None


def test_clear_resets_genre_selections_and_all_biases_together(store):
    seed_genres(store)
    clock = [1000.0]
    client = client_at(store, clock)
    store.rec_model.set_weight("genre:jazz", 1.4, now=clock[0])
    learned = store.rec_model.get_weights(now=clock[0])
    client.post("/home/mix-genres", data={"genres": ["jazz", "techno"]})
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "2"})
    client.post("/home/breadth", data={"breadth_bias": ".8"})
    client.post("/home/fingerprint/add", data={"axis": "genre:gqom"})
    client.post("/home/fingerprint/remove", data={"axis": "genre:techno"})

    response = client.post("/home/fingerprint/reset")
    assert response.status_code == 200
    assert_cleared(store)
    assert store.rec_model.get_weights(now=clock[0]) == learned
    assert 'data-requested-genres=\'[]\'' in response.text
    assert 'hx-swap-oob="outerHTML"' in response.text  # mixes and controls update in one response
    assert "Reset mix adjustments" in response.text
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == 0


@pytest.mark.parametrize("path,data", [
    ("/home/vibe", {"vibe": "gentle"}),
    ("/home/mix-genres", {"genres": "jazz"}),
    ("/home/other-genres", {"genres": '["Jazz"]'}),
    ("/home/breadth", {"breadth_bias": ".8"}),
    ("/home/steer", {"axis": "genre:jazz", "weight": "2"}),
    ("/home/fingerprint/add", {"axis": "genre:gqom"}),
    ("/home/fingerprint/remove", {"axis": "genre:jazz"}),
])
def test_every_control_expires_at_four_hours_without_reads_extending_it(store, path, data):
    seed_genres(store)
    clock = [1000.0]
    client = client_at(store, clock)
    response = client.post(path, data=data)
    assert response.status_code == 200
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == mix_tuning.TTL_S

    clock[0] += mix_tuning.TTL_S - 1
    response = client.get("/home/cards")
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == 1
    assert store.settings.get_setting(mix_tuning.UPDATED_AT_SETTING) == "1000.0"
    # A new client still sees the persisted expiry, rather than starting a new browser timer.
    clock[0] += 1
    response = client_at(store, clock).get("/home/fingerprint")
    assert_cleared(store)
    assert 'data-requested-genres=\'[]\'' in response.text
    assert float(response.headers["X-Mix-Tuning-Remaining"]) == 0


def test_last_adjustment_extends_window_and_new_session_drops_expired_choices(store):
    seed_genres(store)
    clock = [1000.0]
    client = client_at(store, clock)
    client.post("/home/mix-genres", data={"genres": "jazz"})
    clock[0] += 3600
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "2"})
    clock[0] = 1000 + mix_tuning.TTL_S
    client.get("/home/cards")
    assert store.rec_model.get_lean("genre:jazz") == 2
    assert store.rec_surface.get_proposals("home_genre_request")["include_genres"] == ["jazz"]

    clock[0] += 3600
    client.post("/home/breadth", data={"breadth_bias": ".5"})
    assert store.rec_model.get_leans() == {}
    assert store.rec_surface.get_proposals("home_genre_request") == {}
    assert rec_params.get_param(store, "breadth_bias") == .5
    assert mix_tuning.remaining(store, clock[0]) == mix_tuning.TTL_S


@pytest.mark.parametrize("consumer", ["cards", "scoring", "discovery", "graduation"])
def test_expired_biases_stop_affecting_recommendations_without_opening_controls(store, consumer):
    clock = [1000.0]
    client = client_at(store, clock)
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


def test_temporary_bias_does_not_become_learned_taste_across_midnight(store):
    clock = [86400.0 - 60]
    client = client_at(store, clock)
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "2"})
    graduation.graduate_slider_exposure(store, clock[0])
    clock[0] += 120
    graduation.graduate_slider_exposure(store, clock[0])
    assert store.rec_surface.theme_rows() == []
    assert store.rec_model.get_weights(now=clock[0]) == {}
