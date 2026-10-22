import pytest

from yt_playlist.rec import embed, scoring, surfaces


def _model(store, album=None, thumbnail=None):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = [store.tracks.upsert_track(f"a{i}", f"A{i}", "Alpha", album, None, thumbnail=thumbnail) for i in range(8)]
    b = [store.tracks.upsert_track(f"b{i}", f"B{i}", "Beta", album, None, thumbnail=thumbnail) for i in range(8)]
    for track in a:
        store.tracks.set_track_genre(track, "Techno")
    for track in b:
        store.tracks.set_track_genre(track, "Folk")
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "Night drive", 8, "a", 1.0), a)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "Sunday", 8, "b", 1.0), b)
    store.history.add_history_snapshot(iid, 100.0, [f"a{i}|alpha" for i in range(4)])
    embed.build_and_store(store, dim=4)
    keys, V, idx = embed.load_vectors(store)
    return scoring.playlist_taste(store), keys, V, idx


def test_trace_final_scores_equal_production_scores(store):
    taste, keys, V, idx = _model(store)
    store.rec_model.set_weight("genre:techno", 1.4, now=100.0)

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
            + trace["session_delta"] + trace["audio_delta"] + trace["negative_delta"])


def test_taste_sample_items_carry_trace_but_normal_serving_does_not(store):
    _model(store)

    normal = surfaces.for_you(store, now=100.0, limit=4)
    observed = surfaces.taste_sample(store, now=100.0, limit=4)

    assert normal and all(item.trace is None for item in normal)
    assert observed and all(item.trace is not None for item in observed)
    assert all("contexts" in item.trace for item in observed)


def test_taste_comparison_retains_suppressed_baseline_track_with_reason(store):
    _model(store, album="Comparison album", thumbnail="/static/comparison-cover.jpg")
    baseline = surfaces.taste_comparison(store, now=100.0, limit=6)
    assert baseline and all(row["in_cohort"] for row in baseline)
    key = baseline[0]["key"]
    metadata = store.rec_query.tracks_by_keys([key])[key]
    store.rec_model.record_feedback("for_you", key, "dismiss", until=1000.0, now=101.0)

    current = surfaces.taste_comparison(store, now=102.0, keys=[key], limit=6)
    retained = next(row for row in current if row["key"] == key)

    assert retained["in_cohort"] is False
    assert retained["eligible"] is False
    assert retained["eligibility_reason"] == "dismissed or snoozed"
    assert retained["trace"]["final"] == pytest.approx(retained["score"])
    # A track that leaves the shortlist still has the artwork and identity needed to hear it.
    assert retained["video_id"] == metadata["video_id"]
    assert retained["album"] == "Comparison album"
    assert retained["thumbnail"] == "/static/comparison-cover.jpg"
