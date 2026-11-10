"""#84: explicit dislikes remain bounded vector evidence after transient signals fade."""
import numpy as np
from tests.retained_rating_support import apply_model_ratings
from tests.repository_mock_support import no_database, repository_store, transient_store
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import mode_surfaces, negative, rec_params, scoring, transient


def test_dislike_direction_survives_restart_pruning_and_ban_expiry(tmp_path, consumer_store, request):
    path = tmp_path / "taste.db"
    store = consumer_store(path)
    tid = store.tracks.upsert_track("bad", "Bad", "Artist", None, None)
    store.tracks.set_track_genre(tid, "jazz")
    key = store.tracks.identity_key_for_video("bad")
    apply_model_ratings(store, {key: "DISLIKE"}, now=100)
    store.rec_signals.record_mood([key], -1, now=100)
    V = np.array([[1., 0.], [0.9, 0.1], [0., 1.], [-1., 0.]])
    idx = {key: 0, "similar": 1, "unrelated": 2, "opposite": 3}
    fresh = scoring._mood_components(np.zeros(4), store, 100, V, idx)
    store.conn.close()

    store = Store(path)
    request.addfinalizer(store.conn.close)
    store.init_schema()
    later = 100 + 2000 * 86400
    store.rec_signals.record_mood([], 1, now=later, prune_before=later - 1)
    assert key not in store.rec_model.suppressed_keys("for_you", later)
    assert abs(transient.facet_leans(store, later)["genre:jazz"]) < 1e-10
    aged = scoring._mood_components(np.zeros(4), store, later, V, idx)
    assert aged["negative"] == pytest.approx(fresh["negative"])
    assert aged["negative"][1] < 0
    assert aged["negative"][2] == aged["negative"][3] == 0
    assert aged["combined"] == pytest.approx(aged["negative"])

    apply_model_ratings(store, {key: "INDIFFERENT"}, later)
    assert negative.penalties(store, V, idx) == pytest.approx(np.zeros(4))
    store.conn.close()


def test_negative_vectors_are_bounded_missing_and_zero_are_neutral(no_database):
    store = repository_store(settings={'get_setting': None},
                            rec_model={'disliked_identity_keys': {'bad', 'zero', 'missing'}})
    V = np.array([[10., 0.], [0., 0.], [0., 3.], [-10., 0.]])
    idx = {"bad": 0, "zero": 1, "other": 2, "opposite": 3}
    deltas = negative.penalties(store, V, idx)
    weight = rec_params.get_param(store, "dislike_durable_w")
    assert deltas == pytest.approx([-weight, 0, 0, 0])
    store.rec_model.disliked_identity_keys.return_value.add('opposite')
    assert negative.penalties(store, V, idx) == pytest.approx(np.zeros(4))
    store.settings.get_setting.return_value = '0'
    assert negative.penalties(store, V, idx) == pytest.approx(np.zeros(4))


def test_cold_candidates_use_dislike_evidence_outside_the_candidate_pool(transient_store):
    store = transient_store
    store.rec_model.disliked_identity_keys.return_value = {'warm-dislike'}
    evidence = np.array([[1., 0.], [0., 1.]])
    V = np.array([[0.9, 0.1], [0., 1.]])
    components = scoring._mood_components([0.5, 0.5], store, 100, V,
        {"cold-similar": 0, "cold-other": 1},
        evidence_vecs=(evidence, {"warm-dislike": 0, "warm-other": 1}))
    assert components["combined"][0] < components["combined"][1]


@pytest.mark.parametrize("ranker", ["cosine", "ppr"])
def test_prepared_card_order_applies_durable_dislike_delta(ranker):
    items = [{"key": "similar"}, {"key": "unrelated"}]
    order = mode_surfaces._order_key(ranker, {"similar": 0, "unrelated": 1,
        "third": 2, "fourth": 3}, {}, {"similar": -0.35})
    assert [item["key"] for item in sorted(items, key=order)] == ["unrelated", "similar"]


def test_live_steering_streams_distinct_dislikes_and_matches_complete_mean(store, monkeypatch):
    from yt_playlist.controllers.rec_model import RecommendationModel
    keys = [f'bad-{index:03}' for index in range(259)] + ['candidate', 'zero']
    angles = np.linspace(-1.4, 1.4, len(keys))
    vectors = np.column_stack((np.cos(angles), np.sin(angles))).astype(np.float32)
    vectors[-1] = 0
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(identity_key) VALUES (?)', [(key,) for key in keys])
    store.rec_model.replace_rec_vectors([(key, vector.tobytes()) for key, vector in zip(keys, vectors)])
    with store.conn:
        store.conn.executemany("INSERT INTO rec_feedback(surface,item_key,kind,scope,created_at) VALUES (? ,?,'dislike','',10)",
            [('one', key) for key in keys[:259]] + [('two', key) for key in keys[:129]] + [('one', 'zero'), ('one', 'missing')])
    expected = negative.penalties(store, vectors[-2:], {'candidate': 0, 'zero': 1},
                                  evidence_vecs=(vectors, dict(zip(keys, range(len(keys))))))
    calls, original = [], RecommendationModel.vectors_by_keys
    def bounded(self, batch, **kwargs):
        batch = list(batch)
        calls.append(len(batch))
        assert len(batch) <= 128
        return original(self, batch, **kwargs)
    monkeypatch.setattr(RecommendationModel, 'vectors_by_keys', bounded)
    monkeypatch.setattr(RecommendationModel, 'disliked_identity_keys', lambda *args: pytest.fail('Loaded the complete dislike set'))
    with store.read_view() as reader:
        steering = negative.Steering(reader)
        actual = steering.for_keys(['candidate', 'zero', 'absent'])
        assert actual['candidate'] == pytest.approx(expected[0], abs=1e-12)
        assert actual['zero'] == expected[1] == 0 and 'absent' not in actual
        assert len(calls) == 4
        steering.for_keys(['candidate'])
        assert len(calls) == 5


def test_zero_weight_and_empty_candidates_skip_vector_reads(no_database):
    store = repository_store(settings={'get_setting': None},
                            rec_model={'vectors_by_keys': {}, 'disliked_key_page': []})
    assert negative.for_keys(store, []) == {}
    store.settings.get_setting.assert_not_called()
    store.settings.get_setting.return_value = '0'
    assert negative.for_keys(store, ['candidate']) == {}
    store.rec_model.vectors_by_keys.assert_not_called()
    store.rec_model.disliked_key_page.assert_not_called()
