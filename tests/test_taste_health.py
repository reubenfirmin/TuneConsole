"""Route smoke test for the §1 model-health panel (/taste/recall): it must render without a template
error both when the model is empty (no vectors/history) and when a graduation has been logged."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from yt_playlist.rec import graduation, rec_params
from yt_playlist.util.matching import identity_key
from tests.conftest import FakeClient


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0)
    return TestClient(app, base_url="http://127.0.0.1")


def test_health_panel_renders_when_empty(store):
    r = _client(store).get("/taste/recall")
    assert r.status_code == 200
    assert "Not enough playlist data" in r.text
    assert "Not enough history" in r.text
    assert "Not enough track details" in r.text
    assert "No recommendations tracked yet" in r.text
    assert "at least five modeled tracks" in r.text


def test_health_panel_shows_graduation_counts(store):
    tid = store.tracks.upsert_track("v1", "song", "band", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    graduation.graduate_moods(store, [identity_key("song", "band")], 2.0, 1000.0,
                             source=rec_params.SOURCE_W_VIBE, source_label="vibe")
    r = _client(store).get("/taste/recall")
    assert r.status_code == 200
    assert "Saved preference updates" in r.text and "Mood choices" in r.text


def test_health_panel_explains_unknown_historical_play_provenance(store):
    client = _client(store)
    store.rec_surface.record_served_impressions("one", "home", [{
        "lane": "wheelhouse", "identity_key": "song|band", "rank": 1,
    }], 100)
    store.history.import_play_events(store.identities.get_identities()[0].id, [("song|band", "v1", 120)])
    response = client.get("/taste/recall")
    assert response.status_code == 200
    assert "Playlist context wasn’t recorded for plays of 1 recommendation." in response.text
    assert "0 played directly; 0 played through generated playlists." in response.text


def test_health_checks_show_zero_scores_and_explain_samples(store, monkeypatch):
    from yt_playlist.rec import eval_recs

    monkeypatch.setattr(eval_recs, "recall_at_k", lambda *args, **kwargs:
                        {"recall_at_k": 0.0, "k": 20, "trials": 6, "lift": None})
    monkeypatch.setattr(eval_recs, "rolling_temporal_recall", lambda *args, **kwargs:
                        {"recall": 0.0, "k": 20, "trials": 12, "viable_windows": 2,
                         "requested_windows": 3, "generated_excluded": 4})
    monkeypatch.setattr(eval_recs, "projection_recall", lambda *args, **kwargs:
                        {"recall": 0.0, "k": 20, "trials": 24,
                         "breakdown": {"by_coverage": {"genre+year": {"recall": 0.0, "trials": 24}}}})
    monkeypatch.setattr(store.rec_surface, "recommendation_scorecard", lambda *args, **kwargs:
                        {"impressions": 10, "played": 0, "acceptance_rate": 0.0,
                         "organic": 0, "generated": 0, "powered": False, "min_impressions": 50,
                         "open": 0, "start": 0, "save": 0, "promotion": 0, "conversion": 0,
                         "cold_impressions": 0, "unique_artists": 0, "unique_genres": 0,
                         "by_ownership": {}, "by_metadata": {}})
    html = _client(store).get("/taste/recall").text
    assert html.count('class="model-health-reading">0%') == 4
    assert "Not enough" not in html
    assert "12 test tracks across 2 of 3 test periods" in html
    assert "4 tracks played only through generated recommendations were excluded" in html
    assert "Genre, year" in html and "(24 tracks)" in html
    assert "6 playlists tested" in html
    assert "None" not in html
    assert "Early result" in html and "at least 50" in html


def test_health_summary_does_not_treat_missing_variety_as_focused_taste(store):
    html = _client(store).get("/taste/model-status").text
    assert "No track similarity model has been built yet" in html
    assert "Needs listening to genre-tagged tracks" in html
    assert "not model quality" in html
    assert "/100" not in html
