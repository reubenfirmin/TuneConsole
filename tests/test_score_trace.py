import pytest

from yt_playlist.rec import embed, scoring, surfaces


def _model(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = [store.upsert_track(f"a{i}", f"A{i}", "Alpha", None, None) for i in range(8)]
    b = [store.upsert_track(f"b{i}", f"B{i}", "Beta", None, None) for i in range(8)]
    for track in a:
        store.set_track_genre(track, "Techno")
    for track in b:
        store.set_track_genre(track, "Folk")
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "Night drive", 8, "a", 1.0), a)
    store.set_playlist_tracks(store.upsert_playlist(iid, "PB", "Sunday", 8, "b", 1.0), b)
    store.add_history_snapshot(iid, 100.0, [f"a{i}|alpha" for i in range(4)])
    embed.build_and_store(store, dim=4)
    keys, V, idx = embed.load_vectors(store)
    return scoring.playlist_taste(store), keys, V, idx


def test_trace_final_scores_equal_production_scores(store):
    taste, keys, V, idx = _model(store)
    store.set_weight("genre:techno", 1.4, now=100.0)

    served = surfaces._score_candidates(store, taste, keys, V, idx, now=100.0)
    traced, traces = surfaces._score_candidates(
        store, taste, keys, V, idx, now=100.0, include_traces=True)

    assert traced == pytest.approx(served)
    assert set(traces) == set(keys)
    for key, trace in traces.items():
        assert trace["final"] == pytest.approx(served[key])
        assert trace["final"] == pytest.approx(trace["rank_base"] * trace["axis_applied"])
        assert trace["combined"] == pytest.approx(
            trace["durable"] + trace["transient_delta"]
            + trace["session_delta"] + trace["audio_delta"])


def test_taste_sample_items_carry_trace_but_normal_serving_does_not(store):
    _model(store)

    normal = surfaces.for_you(store, now=100.0, limit=4)
    observed = surfaces.taste_sample(store, now=100.0, limit=4)

    assert normal and all(item.trace is None for item in normal)
    assert observed and all(item.trace is not None for item in observed)
    assert all("contexts" in item.trace for item in observed)
