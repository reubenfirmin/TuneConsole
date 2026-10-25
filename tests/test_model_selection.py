import logging
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.rec import embedding_training as embed, model_selection as ms, model_setup
from tests import model_support
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.util.matching import identity_key


def _outcomes(hits, n=40):
    return {str(i): int(i < hits) for i in range(n)}


def _result(now=1000, status="applied"):
    return {"ran_at": now, "status": status, "previous": {"method": "svd", "dim": 48},
            "selected": {"method": "svd", "dim": 64}, "reason": "Measured improvement", "grid": []}


def test_switch_requires_reliable_paired_gain_across_multiple_periods():
    previous = [_outcomes(5), _outcomes(5)]
    assert ms._paired(previous, [_outcomes(25), _outcomes(25)], 5)["promote"]
    assert not ms._paired(previous, [_outcomes(6), _outcomes(6)], 5)["promote"]
    assert not ms._paired(previous, [_outcomes(40), _outcomes(4)], 5)["promote"]
    assert not ms._paired(previous[:1], [_outcomes(40)], 5)["promote"]
    assert not ms._paired(previous, [{}, _outcomes(40)], 5)["eligible"]
    assert not ms._paired(previous, [{"0": 1}, _outcomes(40)], 5)["eligible"]


def test_readiness_explains_initial_wait_pause_cooldown_and_fresh_data(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    assert ms.readiness(store, 1000)["state"] == "waiting"
    store.rec_model.replace_rec_vectors([(str(i), np.ones(2, dtype=np.float32).tobytes()) for i in range(100)])
    store.history.record_history_plays(iid, 0, ["context"], source_timezone="UTC")
    store.history.record_history_plays(iid, 15 * ms.DAY, ["later"], source_timezone="UTC")
    now = 16 * ms.DAY
    assert ms.readiness(store, now)["state"] == "ready"
    result = _result(now, "retained")
    result["evidence_end"] = 15 * ms.DAY
    store.model_selection.start_selection(now, result)
    assert "next_at" in ms.readiness(store, now)
    later = now + 29 * ms.DAY
    assert "fresh listening" in ms.readiness(store, later)["reason"]
    store.history.record_history_plays(iid, later - 1, [f"fresh{i}" for i in range(30)], source_timezone="UTC")
    assert ms.readiness(store, later)["state"] == "ready"
    store.settings.set_setting("rec_selection_paused", "1")
    assert ms.readiness(store, later)["state"] == "paused"


def test_diagnostic_winner_cannot_publish_without_validated_product_outcomes(store, monkeypatch):
    old = [("old|artist", np.array([1, 0], dtype=np.float32).tobytes())]
    store.rec_model.replace_rec_vectors(old)
    store.rec_model.set_weight("genre:jazz", 1.4, now=1)
    store.rec_model.record_like("liked|artist", now=1)
    monkeypatch.setattr(ms, "_windows", lambda *a: ([{}, {}], 100))
    calls = []

    def evaluate(setup, windows):
        assert store.rec_model.get_rec_vectors() == old
        assert store.settings.get_setting("rec_dim") is None
        calls.append(setup)
        return [_outcomes(25 if setup["dim"] == 64 else 5)] * 2

    monkeypatch.setattr(ms, "_evaluate", evaluate)
    monkeypatch.setattr(model_setup, "build", lambda *a: pytest.fail("diagnostic fitted a serving model"))
    monkeypatch.setattr(ms, "_forecast", lambda *a: None)
    result = ms.run(store, 1000)
    assert result["status"] == "retained"
    assert result["diagnostic_challenger"] == {"method": "svd", "dim": 64}
    assert result["promotion_gate"] == "diagnostic_only"
    assert result["selected"] == result["previous"]
    assert store.settings.get_setting("rec_dim") is None
    assert store.rec_model.get_rec_vectors() == old
    assert len(calls) == 5
    assert store.rec_model.weight_records()["genre:jazz"]["weight"] == 1.4
    assert store.rec_model.recent_liked_keys() == ["liked|artist"]
    run_id = store.model_selection.selection_runs()[0]["id"]
    assert not store.model_selection.restore_model_selection(run_id, 1100)
    assert store.rec_model.get_rec_vectors() == old
    assert store.settings.get_setting("rec_dim") is None


def test_failure_during_candidate_fit_preserves_previous_model(store, monkeypatch):
    old = [("old", b"original")]
    store.rec_model.replace_rec_vectors(old)
    monkeypatch.setattr(ms, "_windows", lambda *a: ([{}, {}], 100))
    monkeypatch.setattr(ms, "_evaluate", lambda *a: (_ for _ in ()).throw(RuntimeError("fit failed")))
    with pytest.raises(RuntimeError):
        ms.run(store, 1000)
    assert store.rec_model.get_rec_vectors() == old
    assert store.settings.get_setting("rec_dim") is None
    assert store.model_selection.selection_runs()[0]["status"] == "failed"


def test_inconclusive_tests_keep_current_setup_and_record_reason(store, monkeypatch):
    store.rec_model.replace_rec_vectors([("old", b"original")])
    monkeypatch.setattr(ms, "_windows", lambda *a: ([{}, {}], 100))
    monkeypatch.setattr(ms, "_evaluate", lambda *a: [_outcomes(5), _outcomes(5)])
    monkeypatch.setattr(ms, "_forecast", lambda *a: None)
    assert ms.run(store, 1000)["status"] == "retained"
    assert store.rec_model.get_rec_vectors() == [("old", b"original")]
    monkeypatch.setattr(ms, "_evaluate", lambda *a: [{}, {}])
    assert ms.run(store, 2000)["status"] == "insufficient"


def test_later_measurements_use_saved_forecasts_and_exact_future_play_times(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    run_id = store.model_selection.start_selection(1000, _result())
    store.model_selection.save_model_forecast(run_id, 1000, {"eligible": ["a", "b", "gen"], "previous": ["a"], "selected": ["b"]})
    store.model_selection.save_model_forecast(run_id, 1000 + ms.DAY, {"eligible": ["a", "b", "c"], "previous": ["a"], "selected": ["c"]})
    store.playlists.upsert_playlist(iid, "PLGEN", "Generated", 0, "h", 0)
    store.playlists.set_playlist_group("PLGEN", "Generated")
    store.playlists.mark_generated_playlist('PLGEN')
    for key, at in [("past", 900), ("a", 1010), ("b", 1020), ("b", 1010 + ms.DAY), ("c", 1020 + ms.DAY)]:
        store.history.record_play_event(iid, key, key, at)
    store.history.record_play_event(iid, "gen", "gen", 1030, playlist_ytm_id="PLGEN")
    monkeypatch.setattr(ms, "_forecast", lambda *a: None)
    ms.update_followup(store, 1000 + 2 * ms.DAY)
    f = store.model_selection.selection_runs()[0]["followup"]
    assert f["trials"] == 3
    assert f["previous_hits"] == 1
    assert f["selected_hits"] == 2
    assert f["generated_excluded"] == 1
    assert len(f["points"]) == 2
    # An earlier import cannot become new evidence; it can correctly remove an ineligible repeat.
    store.history.import_play_events(iid, [("c", "c", 800)])
    ms.update_followup(store, 1000 + 2 * ms.DAY)
    revised = store.model_selection.selection_runs()[0]["followup"]
    assert revised["trials"] == 2
    assert revised["selected_hits"] == revised["previous_hits"] == 1


def test_forecast_has_same_universe_and_does_not_replace_live_vectors(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tracks = [store.tracks.upsert_track(str(i), f"Song{i}", "Artist", None, None, created_at=0) for i in range(40)]
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PL", "Mix", 40, "h", 0), tracks)
    store.history.record_play_event(iid,'song0|artist','0',100)
    store.rec_model.replace_rec_vectors([("unchanged", b"original")])
    forecast = ms._forecast(store, {"method": "svd", "dim": 4}, {"method": "svd", "dim": 8}, 1000)
    assert "song0|artist" not in forecast["eligible"]
    assert len(forecast["previous"]) == len(forecast["selected"]) == 20
    assert set(forecast["previous"] + forecast["selected"]) <= set(forecast["eligible"])
    assert store.rec_model.get_rec_vectors() == [("unchanged", b"original")]


def test_startup_recovers_interrupted_comparison_and_unchanged_refresh_reuses_models(store, monkeypatch):
    store.model_selection.start_selection(100, _result(100, "running"))
    store.tracks.upsert_track("warm", "Warm", "Artist", None, None)
    warm_key = identity_key("Warm", "Artist")
    warm_vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32).tobytes()
    store.rec_model.replace_rec_vectors([(warm_key, warm_vector)])
    store.tracks.upsert_track("new", "New", "Other Artist", None, None)
    new_key = identity_key("New", "Other Artist")
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: 1000, logger=logging.getLogger("test")))
    assert store.model_selection.selection_runs()[0]["status"] == "failed"
    monkeypatch.setattr(ms, "maintain", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_start_loop", lambda: None)
    try:
        worker._do_rebuild()
        before = store.rec_model.content_generation()
        def forbidden(*args, **kwargs):
            raise AssertionError("Unchanged inputs must reuse the completed model")
        monkeypatch.setattr(embed, "build_vectors_from_baskets", forbidden)
        worker._do_rebuild()
        assert store.rec_model.content_generation() == before
        assert not store.rec_refresh.has_pending_changes()
        assert len(store.model_selection.selection_runs()) == 1
    finally:
        worker.shutdown()


def test_real_history_drives_automatic_comparison_without_manual_action(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    all_keys = []
    for group in range(8):
        tracks, keys = [], []
        for i in range(20):
            tracks.append(store.tracks.upsert_track(f"{group}-{i}", f"Song{group}_{i}", f"Artist{group}", None, None, created_at=0))
            keys.append(identity_key(f"Song{group}_{i}", f"Artist{group}"))
        all_keys.append(keys)
        store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, f"PL{group}", "Mix", 20, str(group), 0), tracks)
    for period in range(4):
        store.history.record_history_plays(iid, period * 10 * ms.DAY,
            [key for keys in all_keys for key in keys[period * 5:period * 5 + 5]], source_timezone="UTC")
    store.settings.set_setting("rec_dim", "4")
    model_support.build_track_model(store)
    monkeypatch.setattr(model_setup, "CONFIGS", [("svd", 4), ("svd", 8)])
    now = 31 * ms.DAY
    assert ms.readiness(store, now)["state"] == "ready"
    result = ms.maintain(store, now)
    assert result['status'] == 'running', result
    assert result['kind'] == 'product' and result['phase'] == 'observing'
    assert result['challenger'] == {'method': 'svd', 'dim': 8}
    from yt_playlist.rec import product_comparison
    trial = product_comparison.state(store)
    assert trial['decision_at'] > trial['exposure_until'] > now
    assert trial['previous'] == {'method': 'svd', 'dim': 4}
    assert store.rec_model.rec_vectors_count() == 160
    assert ms.maintain(store, now + 1) is None
    assert len(store.model_selection.selection_runs()) == 1


def test_measurement_and_restore_survive_later_checks_that_keep_the_selected_setup(store, monkeypatch):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.rec_model.replace_rec_vectors([("old", b"previous snapshot")])
    applied = _result()
    first = store.model_selection.start_selection(1000, applied)
    store.model_selection.save_selection(first, applied, rows=[("new", b"selected snapshot")])
    store.model_selection.save_model_forecast(first, 1000, {"eligible": ["later"], "previous": [], "selected": ["later"]})
    latest = _result(2000, "retained")
    latest["previous"] = latest["selected"]
    second = store.model_selection.start_selection(2000, latest)
    store.history.record_play_event(iid, "later", "later", 2500)
    monkeypatch.setattr(ms, "_forecast", lambda *a: None)
    ms.update_followup(store, 3000)
    assert store.model_selection.selection_run(first)["followup"]["selected_hits"] == 1
    view = ms.view(store, 3000)
    assert view["last"]["id"] == first
    assert view["latest"]["id"] == second
    assert store.model_selection.restore_model_selection(first, 3500)
    assert store.rec_model.get_rec_vectors() == [("old", b"previous snapshot")]
    assert ms.view(store, 4000)["last"]["status"] == "restored"


def test_publication_failure_rolls_back_vectors_and_configuration(store):
    import sqlite3
    store.rec_model.replace_rec_vectors([("old", b"previous")])
    result = _result()
    run_id = store.model_selection.start_selection(1000, result)
    with pytest.raises(sqlite3.IntegrityError):
        store.model_selection.save_selection(run_id, result, rows=[("duplicate", b"a"), ("duplicate", b"b")])
    assert store.rec_model.get_rec_vectors() == [("old", b"previous")]
    assert store.settings.get_setting("rec_dim") is None
    assert store.settings.get_setting("rec_selection_active_run") is None
