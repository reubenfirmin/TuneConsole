from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.rec import rec_params
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1"), iid


def test_recs_journey_nudges_journey_weight(store):
    c, iid = _client(store)
    pid = store.upsert_playlist(iid, "PLG", "Comfort - June 22 2026", 1, "h", 1.0)
    store.set_playlist_group("PLG", "Generated")
    store.set_recipe("PLG", {"journey": "energy_arc", "facets": {}, "dj": {"seed": 1}}, 1.0)

    for _ in range(int(rec_params.THEME_THRESHOLD) + 1):
        r = c.post("/recs/journey", data={"pid": pid, "dir": 1})
        assert r.status_code == 200
    # #85 the route's now_fn is fixed at 1.0; read at the same `now` so reversion (vs real wall-clock
    # time) doesn't erase the nudge before the assertion runs.
    assert store.get_weights(now=1.0).get("journey:energy_arc", 1.0) > 1.0     # raised its weight


def test_recs_journey_bad_input_is_422(store):
    c, _ = _client(store)
    assert c.post("/recs/journey", data={"dir": 1}).status_code == 422   # no pid


def test_playlist_detail_renders_flow_label(store):
    c, iid = _client(store)
    t = store.upsert_track("v1", "S1", "A", "", None, 1)
    pid = store.upsert_playlist(iid, "PLG", "Comfort - June 22 2026", 1, "h", 1.0)
    store.set_playlist_tracks(pid, [t])
    store.set_playlist_group("PLG", "Generated")
    store.set_recipe("PLG", {"journey": "energy_arc", "facets": {}, "dj": {"seed": 1}}, 1.0)

    html = c.get(f"/playlist/{pid}").text
    assert "Energy arc" in html and "/recs/journey" in html      # Flow label + feedback control
    assert "builds to a peak" in html        # journey description shown under the lever
    assert 'x-data' in html and "sel === 1" in html       # levers keep a sticky selected state
