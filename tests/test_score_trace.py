import numpy as np
import pytest

from yt_playlist.rec import embedding_vectors as embed, scoring, surfaces
from tests import model_support
from tests.retained_rating_support import apply_model_ratings


def _model(store, album=None, thumbnail=None):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    a = [store.tracks.upsert_track(f"a{i}", f"A{i}", "Alpha", album, None, thumbnail=thumbnail) for i in range(8)]
    b = [store.tracks.upsert_track(f"b{i}", f"B{i}", "Beta", album, None, thumbnail=thumbnail) for i in range(8)]
    for track in a:
        store.tracks.set_track_genre(track, "Techno")
    for track in b:
        store.tracks.set_track_genre(track, "Folk")
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PA", "Night drive", 8, "a", 1.0), a)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PB", "Sunday", 8, "b", 1.0), b)
    store.history.record_history_plays(iid, 100.0, [f"a{i}|alpha" for i in range(4)], source_timezone="UTC")
    model_support.build_track_model(store, dim=4)
    keys, V, idx = embed.load_vectors(store)
    return scoring.playlist_taste(store), keys, V, idx


def test_trace_final_scores_equal_production_scores(store):
    taste, keys, V, idx = _model(store)
    apply_model_ratings(store, {keys[0]: "DISLIKE"}, now=1.0)
    store.rec_model.set_weight("genre:techno", 1.4, now=100.0)

    served = surfaces._score_candidates(store, taste, keys, V, idx, now=100.0)
    traced, traces = surfaces._score_candidates(
        store, taste, keys, V, idx, now=100.0, include_traces=True)

    assert traced == pytest.approx(served)
    assert traces[keys[0]]["negative_delta"] < 0
    assert set(traces) == set(keys)
    for key, trace in traces.items():
        assert trace["final"] == pytest.approx(served[key])
        assert trace["final"] == pytest.approx(trace["rank_base"] * trace["axis_applied"])
        assert trace["combined"] == pytest.approx(
            trace["durable"] + trace["transient_delta"]
            + trace["session_delta"] + trace["audio_delta"] + trace["negative_delta"])


def test_taste_sample_items_carry_trace_but_normal_serving_does_not(store):
    _, keys, vectors, index = _model(store)
    taste = scoring.playlist_taste(store, now=100.0)
    assert set(taste.titles) == {"Night drive", "Sunday"}

    # Include the complete fitted pool so both explanation cases are exercised
    # every time, rather than depending on the random four-item sample.
    normal = surfaces.for_you(store, now=100.0, limit=len(keys))
    observed = surfaces.taste_sample(store, now=100.0, limit=len(keys))

    assert normal and all(item.trace is None for item in normal)
    assert observed and all(item.trace is not None for item in observed)
    assert [item.key for item in observed] == [item.key for item in normal]
    assert [item.score for item in observed] == pytest.approx([item.score for item in normal])
    supported, unsupported = [], []
    for item in observed:
        vector = vectors[index[item.key]]
        assert np.linalg.norm(vector) > 0
        positive = sorted(
            ((name, float(weight * np.dot(centroid, vector) / np.linalg.norm(vector)))
             for name, centroid, weight in zip(taste.titles, taste.centroids, taste.weights)),
            key=lambda row: -row[1])
        positive = [(name, value) for name, value in positive if value > 0][:3]
        contexts = item.trace["contexts"]
        if not positive:
            assert contexts == []
            unsupported.append(item.key)
            continue
        supported.append(item.key)
        assert 0 < len(contexts) <= 3
        assert [row["name"] for row in contexts] == [name for name, _ in positive]
        assert [row["contribution"] for row in contexts] == pytest.approx(
            [value for _, value in positive])
        assert all(row["contribution"] > 0 for row in contexts)
        assert [row["contribution"] for row in contexts] == sorted(
            (row["contribution"] for row in contexts), reverse=True)
    assert supported and unsupported


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
