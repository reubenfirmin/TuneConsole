"""Route smoke test for the §1 model-health panel (/taste/recall): it must render without a template
error both when the model is empty (no vectors/history) and when a graduation has been logged."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.rec import recommend, rec_params
from yt_playlist.util.matching import identity_key
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1")


def test_health_panel_renders_when_empty(store):
    r = _client(store).get("/taste/recall")
    assert r.status_code == 200
    assert "recall@20" in r.text                       # the placeholder copy for a vectorless model


def test_health_panel_shows_graduation_counts(store):
    tid = store.upsert_track("v1", "song", "band", None, None)
    store.set_track_genre(tid, "Techno")
    recommend.graduate_moods(store, [identity_key("song", "band")], 2.0, 1000.0,
                             source=rec_params.SOURCE_W_VIBE, source_label="vibe")
    r = _client(store).get("/taste/recall")
    assert r.status_code == 200
    assert "graduations" in r.text and "vibe" in r.text
