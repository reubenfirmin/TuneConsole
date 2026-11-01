"""Teacher fit dependencies are distinct from complete measurement lineage."""
import numpy as np

from experiments.audio_lab_tools import external_local_ablation as ablation
from experiments.audio_lab_tools import external_local_pool as pool
from experiments.audio_lab_tools import external_multitask as shared
from experiments.audio_lab_tools.external_experiment import digest, save


def test_current_teacher_retains_native_and_local_fit_dependencies(tmp_path, monkeypatch):
    snapshot = tmp_path / 'historical.json'
    save(snapshot, {'gold': [{'video_id': 'known-local', 'embedding_values': [0.] * 200}],
        'teacher': {'model_sha256': 'old-model', 'training_video_ids': ['old-local']}})
    product = tmp_path / 'src/yt_playlist/rec/data/emotify-vibes.json'
    save(product, {'provenance': {'snapshot_sha256': digest(snapshot)}})
    monkeypatch.setattr(pool, 'ROOT', tmp_path)
    monkeypatch.setattr(pool, 'SNAPSHOT', snapshot)
    rows = [{'source': 'audio_measurement', 'model_version': 'prefix/' + digest(product)}]
    provenance = pool.teacher_training_provenance(rows)
    assert provenance['native_training_corpora'] == ['emotify']
    assert provenance['local_training_video_ids'] == ['known-local']
    assert provenance['known_fit_dependencies_complete']
    # Provider resolution does not invoke the audio model stamped in its version.
    assert pool.teacher_training_provenance([
        {'source': 'provider_measurement', 'model_version': 'prefix/' + digest(product)}])['local_training_video_ids'] == []
    assert not pool.teacher_training_provenance([
        {'source': 'audio_measurement', 'model_version': 'unresolved'}])['known_fit_dependencies_complete']


def test_target_trained_teacher_is_excluded_but_real_unlabeled_input_is_retained():
    local = {'recording_id': 1, 'artist': 'Different artist', 'title': 'Different song',
        'teacher': {'lineage_complete': True, 'scores': [.5] * 6, 'contributor_recording_ids': [1],
            'training_provenance': {'known_fit_dependencies_complete': True,
                'native_training_corpora': ['emotify'], 'local_training_video_ids': []}}}
    selected, teachers, rejected = ablation.disjoint_pool('emotify-artist',
        [{'artist': 'Target artist', 'title': 'Target song'}], [local],
        {'recordings': [{'id': 1, 'artist': 'Different artist', 'title': 'Different song'}]}, {'gold': []})
    assert selected == [local]
    assert teachers == []
    assert rejected[0]['reason'] == 'teacher_fitted_on_target_corpus'


def test_zero_auxiliary_weight_does_not_read_or_standardize_local_inputs(monkeypatch):
    monkeypatch.setattr(shared, 'STEPS', 5)
    x = np.arange(36, dtype=float).reshape(12, 3) / 10
    y = np.linspace(.1, .9, 12)[:, None]
    first = ablation.shared_model(x, y, np.ones((2, 3)), np.zeros((2, 6)), 0., .001)
    second = ablation.shared_model(x, y, np.full((2, 3), 1e9), np.ones((2, 6)), 0., .001)
    assert np.array_equal(shared.prediction(first, x), shared.prediction(second, x))
    assert np.array_equal(first['mean'], x.mean(0))


def test_zero_distillation_weight_matches_native_ridge():
    from experiments.audio_lab_tools.external_training import fit_path, predict
    rng = np.random.default_rng(42)
    x, local = rng.normal(size=(15, 4)), rng.normal(size=(3, 4))
    y = rng.uniform(size=(15, 2))
    candidates = ablation.distillation_candidates(x, y, local, np.ones((3, 2)))
    for model in fit_path(x, y):
        assert np.allclose(predict(candidates[0., model['alpha']], x), predict(model, x), atol=1e-12)


def test_normalization_control_includes_inputs_without_auxiliary_targets(monkeypatch):
    monkeypatch.setattr(shared, 'STEPS', 5)
    x = np.arange(36, dtype=float).reshape(12, 3) / 10
    y = np.linspace(.1, .9, 12)[:, None]
    local = np.full((2, 3), 10.)
    model = ablation.shared_model(x, y, local, np.ones((2, 6)), 0., .001, normalize_only=True)
    assert np.allclose(model['mean'], np.vstack([x, local]).mean(0))
    assert np.count_nonzero(model['parameters']['output_weights'][:, 1:]) == 0
