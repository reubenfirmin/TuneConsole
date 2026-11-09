import numpy as np
import pytest

from yt_playlist.rec import embedding_vectors as embed, scoring, surfaces
from tests import model_support
from tests.recommendation_support import seed_publication_subjects
from tests.retained_rating_support import apply_model_ratings
from tests.store_fixture_support import native_sql_calls, without_query_timing


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Scoring traces retain actual model fitting and SQL without HTTP startup."""


def _model(store, album=None, thumbnail=None):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    # Scoring consumes completed catalog facts; provider ingestion is below its
    # boundary. Keep all sixteen songs and the actual playlist/history fit.
    seed_publication_subjects(store, [
        dict(key=f'{prefix}{i}|{artist.lower()}', video_id=f'{prefix}{i}',
             title=f'{prefix.upper()}{i}', artist=artist, album=album, genre=genre)
        for prefix, artist, genre in (('a', 'Alpha', 'Techno'), ('b', 'Beta', 'Folk'))
        for i in range(8)])
    with store.conn:
        store.conn.execute('UPDATE youtube_items SET thumbnail=?', (thumbnail,))
    entries = dict(store.conn.execute('SELECT video_id,id FROM catalog_entries'))
    a = [entries[f'a{i}'] for i in range(8)]
    b = [entries[f'b{i}'] for i in range(8)]
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

    # Keep all fitted candidates and add an explicit nonzero vector opposite
    # the listened-to context. SVD need not put any Sunday member on that side
    # of Night drive, and Sunday has no listening weight of its own.
    unsupported_key = 'outsider|gamma'
    opposite = -taste.centroids[taste.titles.index('Night drive')]
    assert np.linalg.norm(opposite) > 0
    contributions = [float(weight * np.dot(centroid, opposite))
                     for centroid, weight in zip(taste.centroids, taste.weights)]
    assert max(contributions) <= 0 and min(contributions) < 0
    seed_publication_subjects(store, [dict(key=unsupported_key, video_id='outsider',
                                         title='Outsider', artist='Gamma', genre='Folk')])
    store.rec_model.replace_rec_vectors([
        *[(key, vectors[index[key]].tobytes()) for key in keys],
        (unsupported_key, opposite.astype(np.float32).tobytes())])
    keys, vectors, index = embed.load_vectors(store)
    taste = scoring.playlist_taste(store, now=100.0)

    # Include the complete pool so the deliberately unsupported candidate is
    # scored and served by the same real paths as its fitted neighbors.
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
    assert supported and unsupported_key in unsupported


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
