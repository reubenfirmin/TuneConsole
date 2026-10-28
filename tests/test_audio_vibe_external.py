"""Bundled model inference, compatibility, and background publication."""
from copy import deepcopy
import json

import pytest

from tests.test_audio_vibe_embeddings import with_embedding
from tests.test_audio_vibe_model import excerpts
from tests.rec.test_vibe_prediction import drain, fits, track
from yt_playlist.core import audio_embedding, features
from yt_playlist.rec import audio_vibe_external as external
from yt_playlist.rec import audio_vibe_model as model, audio_vibe_preview as preview
from yt_playlist.rec import vibe_preparation


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
    baseline = preview.predict(samples, use_model=False)
    prediction = preview.predict(samples, metadata={'genre': 'Unknown', 'bpm': 300})
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
    assert preview.predict(samples, metadata={}) == prediction
    assert {r['score_source'] for r in prediction['ranking']} == {'external_emotion_model'}


def test_packaged_default_and_explicit_disable_or_local_override(tmp_path, monkeypatch):
    monkeypatch.delenv(model.ENVIRONMENT_KEY)
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
    assert preview.predict(samples) == preview.predict(samples, use_model=False)


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
    assert preview.predict(samples) == preview.predict(samples, use_model=False)


def test_model_change_replaces_one_measurement_vote_and_preserves_feedback(store, tmp_path, monkeypatch):
    _tid, measured = track(store, 'observed_song')
    _tid, neighbour = track(store, 'neighbour')
    samples = [with_embedding(s, 3.) for s in excerpts()]
    value = controlled(samples)
    path = enable(value, tmp_path, monkeypatch)
    # Startup and incomplete captures remain useful without manufacturing evidence.
    drain(store)
    assert fits(store, measured)['relax']['source'] == 'heuristic'
    for i, sample in enumerate(samples[:2]):
        store.audio_vibes.save_sample(sample, i + 1)
    drain(store)
    assert fits(store, measured)['relax']['source'] == 'heuristic'
    store.audio_vibes.save_sample(samples[2], 3)
    store.audio_vibes.save_feedback(samples[0]['run_id'], [], ['cruise'], preview.TAXONOMY_VERSION, None, 4)
    feedback = store.audio_vibes.feedback(samples[0]['run_id'])
    drain(store)
    before = fits(store, measured)['relax']['score']
    assert fits(store, measured)['relax']['source'] == 'audio_measurement'
    assert fits(store, neighbour)['relax']['recordings'] == 1
    stamp = vibe_preparation.version()
    value['calibration']['intercept'][0] = -2.
    path.write_text(json.dumps(value))
    assert vibe_preparation.version() != stamp
    statements = []
    store.conn.set_trace_callback(statements.append)
    drain(store)
    store.conn.set_trace_callback(None)
    assert fits(store, measured)['relax']['score'] < before
    assert fits(store, neighbour)['relax']['recordings'] == 1
    assert store.audio_vibes.feedback(samples[0]['run_id']) == feedback
    assert not any('audio_session_labels' in sql or 'audio_vibe_feedback' in sql for sql in statements)
