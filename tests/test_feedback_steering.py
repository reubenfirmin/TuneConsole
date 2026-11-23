"""Suggestion feedback adjusts the current listening session and leaves long-term taste alone."""
from fastapi.testclient import TestClient
from tests.enrichment_support import seed_audio
from tests.repository_mock_support import no_database, transient_store

from yt_playlist.rec import rec_params, scoring
from yt_playlist.util import genre_map
from yt_playlist.web.app import create_app


def _post(store, now=1000.0, **data):
    c = TestClient(create_app(store, lambda: {}, now_fn=lambda: now), base_url="http://127.0.0.1")
    return c.post("/recs/feedback", data=data)


def test_wrong_vibe_steers_genre_for_the_current_session(store):
    store.accounts.upsert_account("main", None, True)
    tid = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    fam = genre_map.family("Techno")
    r = _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="vibe")
    assert r.status_code == 200
    assert store.rec_model.get_session_weights(now=1000)[f"genre:{fam}"] == .85
    assert f"genre:{fam}" not in store.rec_model.weight_records()
    assert store.rec_signals.get_theme(f"genre:{fam}") is None


def test_wrong_era_steers_the_decade(store):
    store.accounts.upsert_account("main", None, True)
    tid = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_year(tid, "1995")
    _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="era")
    assert store.rec_model.get_session_weights(now=1000)["era:1990"] == .85


def test_not_this_artist_steers_artist_for_the_current_session(store):
    store.accounts.upsert_account("main", None, True)
    store.tracks.upsert_track("v1", "Song", "Band", None, None)
    _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss",
          reason="artist", axis="artist:Band")                     # the template sends an explicit axis
    assert store.rec_model.get_session_weights(now=1000)["artist:Band"] == .85
    assert "artist:Band" not in store.rec_model.weight_records()


def test_already_know_it_suppresses_without_any_taste_steer(store):
    store.accounts.upsert_account("main", None, True)
    tid = store.tracks.upsert_track("v1", "Song", "Band", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    fam = genre_map.family("Techno")
    _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="own_it")
    assert f"genre:{fam}" not in store.rec_model.get_session_weights(now=1000)
    assert "song|band" in store.rec_model.suppressed_keys("suggest", now=1001.0, scope="7")   # but suppressed


def test_too_mainstream_steers_pop_only_when_track_is_mainstream(store):
    store.accounts.upsert_account("main", None, True)
    thresh = int(rec_params.get_param(store, "pop_mainstream_min"))
    hot = store.tracks.upsert_track("v1", "Hot", "A", None, None)
    seed_audio(store, hot, popularity=thresh + 1000)
    cold = store.tracks.upsert_track("v2", "Cold", "B", None, None)
    seed_audio(store, cold, popularity=1)
    _post(store, item="hot|a", surface="suggest", scope="7", kind="dismiss", reason="mainstream")
    assert store.rec_model.get_session_weights(now=1000)["pop:mainstream"] == .85
    _post(store, item="cold|b", surface="suggest", scope="7", kind="dismiss", reason="mainstream")
    assert store.rec_model.get_session_weights(now=1000)["pop:mainstream"] == .85


def test_why_chip_axis_steers_the_current_session(store):
    store.accounts.upsert_account("main", None, True)
    _post(store, item="x|y", surface="for_you", kind="more", axis="genre:techno")
    assert store.rec_model.get_session_weights(now=1000)["genre:techno"] == 1.15
    assert "genre:techno" not in store.rec_model.weight_records()


def test_pop_band_scorer_downweights_mainstream_tracks(transient_store):
    store = transient_store
    thresh = int(rec_params.get_param(store, "pop_mainstream_min"))
    store.rec_query.track_popularity.return_value = {'hot|a': thresh + 1000, 'cold|b': 1}
    store.rec_query.track_artists.return_value = {'hot|a': 'A', 'cold|b': 'B'}
    store.rec_model.get_weights.return_value = {'pop:mainstream': 0.5}
    mult = scoring._axis_weights_for(store, ["hot|a", "cold|b"])
    assert mult is not None
    assert mult["hot|a"] < mult["cold|b"]                          # the mainstream track ranks lower


def test_vibe_on_untagged_track_is_a_graceful_noop(store):
    store.accounts.upsert_account("main", None, True)
    store.tracks.upsert_track("v1", "Song", "Band", None, None)           # no genre
    r = _post(store, item="song|band", surface="suggest", scope="7", kind="dismiss", reason="vibe")
    assert r.status_code == 200                                    # no axis derived -> no steer, no error
