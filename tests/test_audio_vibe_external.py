"""Bundled model inference, compatibility, and background publication."""
from copy import deepcopy
import json

import pytest

from tests.audio_model_support import disable_models
from tests.test_audio_vibe_cruise import samples as measured_samples
from tests.test_audio_vibe_embeddings import with_embedding
from tests.test_audio_vibe_model import excerpts
from tests.rec.test_vibe_prediction import drain, fits, track
from yt_playlist.core import audio_embedding, features
from experiments.audio_lab_tools.legacy import audio_vibe_external as external
from experiments.audio_lab_tools.legacy import audio_vibe_model as model, audio_vibe_prediction as predictor
from yt_playlist.rec import audio_vibe_relax, vibe_joint_state, vibe_preparation


def bundled():
    return json.loads(model.DEFAULT_PATH.read_text())


def controlled(samples):
    value = bundled()
    value['embedding_signature'] = audio_embedding.signature(samples[0])
    native, calibration = value['native'], value['calibration']
    native.update(mean=[1.] * 200, scale=[2.] * 200,
                  weights=[[0.] * 9 for _ in range(200)], intercept=[0.] * 9)
    native['weights'][0][0] = 2.
    calibration.update(mean=[0.] * 9, scale=[1.] * 9,
                       weights=[[0.] * 6 for _ in range(9)],
                       intercept=[.1, .2, .3, .4, -.5, 2.])
    calibration['weights'][0][0] = 1.
    return value


def enable(value, tmp_path, monkeypatch):
    path = tmp_path / 'external.json'
    path.write_text(json.dumps(value))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    return path


def test_two_stages_clip_before_normalization_and_preserve_raw_evidence(tmp_path, monkeypatch):
    samples = [with_embedding(s, n) for s, n in zip(excerpts(), (1., 3., 5.), strict=True)]
    original = deepcopy(samples)
    value = controlled(samples)
    enable(value, tmp_path, monkeypatch)
    baseline = predictor.predict(samples, use_model=False)
    prediction = predictor.predict(samples, metadata={'genre': 'Unknown', 'bpm': 300})
    # Average the three excerpts before either stage. The native emotion
    # clips to one, and calibration ties Relax and Release before normalization.
    expected = dict(zip(external.LABELS, (1., .2, .3, .4, 1e-8, 1.), strict=True))
    total = sum(expected.values())
    assert {r['vibe']: r['score'] for r in prediction['ranking']} == pytest.approx(
        {key: n / total for key, n in expected.items()})
    assert prediction['selection_method'] == 'top_ranked'
    assert prediction['vibe'] == 'relax', 'Ties follow the evaluated target order'
    assert prediction['model']['sha256'] == model.identity()
    assert prediction['model']['trained_labels'] == sorted(external.LABELS)
    assert prediction['arc'] == baseline['arc']
    assert prediction['dimensions'] == baseline['dimensions']
    assert prediction['calibrated'] is False
    assert samples == original
    assert predictor.predict(samples, metadata={}) == prediction
    assert {r['score_source'] for r in prediction['ranking']} == {'external_emotion_model'}


def test_packaged_default_and_explicit_disable_or_local_override(tmp_path, monkeypatch):
    monkeypatch.delenv(model.ENVIRONMENT_KEY, raising=False)
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    assert model.configured_path() == str(model.DEFAULT_PATH)
    assert model.validate(bundled())['training']['native_songs'] == 400
    assert model.identity() != 'heuristic'
    features.LOCAL_ENV.write_text(model.ENVIRONMENT_KEY + '=\n')
    assert model.configured_path() is None
    features.LOCAL_ENV.write_text(model.ENVIRONMENT_KEY + '=/custom/model.json\n')
    assert model.configured_path() == '/custom/model.json'
    monkeypatch.setenv(model.ENVIRONMENT_KEY, '')
    assert model.configured_path() is None
    assert model.identity() == 'heuristic'


@pytest.mark.parametrize('mutation', ['taxonomy', 'labels', 'emotions', 'signature', 'version',
    'native_width', 'calibration_width', 'native_scale', 'calibration_scale', 'nan', 'boolean',
    'training', 'overflow'])
def test_malformed_external_artifact_falls_back(tmp_path, monkeypatch, mutation):
    samples = [with_embedding(s) for s in excerpts()]
    value = controlled(samples)
    if mutation in ('taxonomy', 'version'):
        value['taxonomy_version' if mutation == 'taxonomy' else 'feature_version'] = 'unknown'
    elif mutation in ('labels', 'emotions'):
        value[mutation].reverse()
    elif mutation == 'signature':
        value['embedding_signature'] = 'g' * 64
    elif mutation.endswith('_width'):
        value[mutation.removesuffix('_width')]['weights'][0].pop()
    elif mutation.endswith('_scale'):
        value[mutation.removesuffix('_scale')]['scale'][0] = 0.
    elif mutation in ('nan', 'boolean'):
        value['native']['weights'][0][0] = float('nan') if mutation == 'nan' else True
    elif mutation == 'training':
        value['training']['songs'] = False
    else:
        value['native']['scale'][0] = 1e-320
    enable(value, tmp_path, monkeypatch)
    assert predictor.predict(samples) == predictor.predict(samples, use_model=False)


@pytest.mark.parametrize('mutation', ['missing', 'hash', 'version', 'length'])
def test_incompatible_capture_falls_back_without_losing_raw_arc(tmp_path, monkeypatch, mutation):
    samples = [with_embedding(s) for s in excerpts()]
    enable(controlled(samples), tmp_path, monkeypatch)
    if mutation == 'missing':
        samples[0].pop('embedding')
    elif mutation == 'hash':
        samples[0]['artifact_hashes']['musicnn/model.json'] = 'c' * 64
    elif mutation == 'version':
        samples[0]['model_version'] = 'future'
    else:
        samples[0]['embedding']['values'].pop()
    assert predictor.predict(samples) == predictor.predict(samples, use_model=False)


def test_retired_model_configuration_cannot_publish_or_replace_measurements(store, tmp_path, monkeypatch):
    _tid, measured = track(store, 'observed_song')
    _tid, neighbour = track(store, 'neighbour')
    samples = measured_samples()
    value = controlled(samples)
    path = enable(value, tmp_path, monkeypatch)
    disable_models(monkeypatch)
    monkeypatch.setattr(vibe_joint_state, 'QUALIFICATIONS', {})
    # Startup and incomplete captures remain useful without manufacturing evidence.
    drain(store).close()
    assert not fits(store, measured)
    for i, sample in enumerate(samples[:2]):
        store.audio_vibes.save_sample(sample, i + 1)
    drain(store).close()
    assert not fits(store, measured)
    store.audio_vibes.save_sample(samples[2], 3)
    store.audio_vibes.save_feedback(samples[0]['run_id'], [], ['cruise'], predictor.TAXONOMY_VERSION, None, 4)
    feedback = store.audio_vibes.feedback(samples[0]['run_id'])
    drain(store).close()
    assert not fits(store, measured) and not fits(store, neighbour)
    assert not store.conn.execute('SELECT 1 FROM measurements').fetchone()
    monkeypatch.setenv(audio_vibe_relax.ENVIRONMENT_KEY, str(audio_vibe_relax.DEFAULT_PATH))
    monkeypatch.setenv(audio_vibe_relax.MODE_KEY, 'active')
    drain(store).close()
    before = fits(store, measured)
    assert before['relax']['source'] == 'audio_measurement' and before['relax']['support'] == 1
    history = list(store.conn.execute('SELECT id FROM measurements ORDER BY id'))
    assert history
    stamp = vibe_preparation.version()
    value['calibration']['intercept'][0] = -2.
    path.write_text(json.dumps(value))
    assert vibe_preparation.version() == stamp
    statements = []
    store.conn.set_trace_callback(statements.append)
    drain(store).close()
    store.conn.set_trace_callback(None)
    assert fits(store, measured) == before
    assert list(store.conn.execute('SELECT id FROM measurements ORDER BY id')) == history
    assert not fits(store, neighbour)
    assert store.audio_vibes.feedback(samples[0]['run_id']) == feedback
    assert not any('audio_annotation' in sql or 'audio_taxonom' in sql for sql in statements)
