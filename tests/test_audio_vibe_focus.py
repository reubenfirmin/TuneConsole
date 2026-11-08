"""Direct Focus integration, six-specialist composition and bounded refresh."""
from copy import deepcopy
import json

import pytest

from tests.rec.test_vibe_prediction import drain, fits
from tests.test_audio_vibe_cruise import samples
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store
from yt_playlist.core import features
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release, audio_vibe_relax as relax
from yt_playlist.rec import audio_vibe_uplift as uplift, audio_vibe_energize as energize, audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor
from yt_playlist.rec import vibe_preparation, vibe_joint_preparation

SPECIALISTS = (cruise, release, relax, uplift, energize, focus)


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('psybient', 'Psybient'),)) as schema:
        yield schema


@pytest.fixture(autouse=True)
def production_models(isolated_app_home, monkeypatch, tmp_path):
    # This specialist's refresh owns measured scores and cohort propagation.
    # The separate joint-model lifecycle owner fits and publishes posteriors.
    monkeypatch.setattr(vibe_joint_preparation, 'qualified', lambda: False)
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    for module in (shared, *SPECIALISTS):
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
    for module in SPECIALISTS:
        monkeypatch.setenv(module.MODE_KEY, 'active')


def baseline(rows, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv(focus.MODE_KEY, 'off')
        return predictor.predict(rows, require_complete=False)


def controlled(tmp_path, values=(.8, 1.)):
    artifact = json.loads(focus.DEFAULT_PATH.read_text())
    artifact['heads'] = [{'kind': 'ridge', 'medians': [0.] * 200, 'mean': [0.] * 200,
                         'scale': [1.] * 200, 'weights': [[0.] for _ in range(200)], 'intercept': [value]}
                        for value in values]
    path = tmp_path / 'focus.json'
    path.write_text(json.dumps(artifact))
    return path


@pytest.mark.parametrize('count', [1, 2, 3])
def test_focus_preserves_five_specialists_and_shared_classification(count, monkeypatch):
    rows = samples()[:count]
    original = deepcopy(rows)
    before, after = baseline(rows, monkeypatch), predictor.predict(rows, require_complete=False)
    assert {k: v for k, v in before.items() if k not in ('suitability', 'individual_models')} == {
        k: v for k, v in after.items() if k not in ('suitability', 'individual_models')}
    assert set(after['individual_models']) == set(shared.LABELS)
    for vibe in shared.LABELS:
        if vibe != focus.VIBE:
            assert after['individual_models'][vibe] == before['individual_models'][vibe]
            assert after['suitability'][vibe] == before['suitability'][vibe]
    assert after['suitability'][focus.VIBE] != before['suitability'][focus.VIBE]
    assert after['individual_models'][focus.VIBE]['sample_count'] == count
    assert all(module.apply(rows, after) == after for module in SPECIALISTS)
    assert rows == original


def test_each_native_head_is_clipped_before_excerpt_and_task_averaging(tmp_path):
    path = controlled(tmp_path, (0., 0.))
    artifact = json.loads(path.read_text())
    artifact['heads'][0]['weights'][0] = [1.]
    artifact['heads'][1]['intercept'] = [.2]
    path.write_text(json.dumps(artifact))
    result = focus.score(focus.load(path), [[2.] + [0.] * 199, [-.5] + [0.] * 199])
    assert result['native'] == {'Usage-Reading': .5, 'Usage-Studying': .2}
    assert result['score'] == .35  # Averaging embeddings first would yield .475.


def test_exact_candidate_provenance():
    header = focus.load(focus.DEFAULT_PATH)['header']
    assert header['source_model_sha256'] == '39f6741b989bd1d665d4414506230d9b7b936285fbb52bf78b24af30e75da098'
    assert header['source_plan_sha256'] == '5af390b1c1173e4d2978f6ca34835b955a4d81a6206c71965fb855f2d1545486'
    assert header['training_rows'] == 494
    assert header['target_names'] == ['Usage-Reading', 'Usage-Studying']
    assert [h['alpha'] for h in header['heads']] == [1000., 1000.]


@pytest.mark.parametrize('failure', ['missing', 'disabled', 'oversize', 'malformed', 'nesting', 'format', 'signature',
    'taxonomy', 'scale', 'coefficient', 'shape', 'mode', 'weights', 'targets', 'head_count', 'aggregation',
    'training_aggregation', 'rows', 'feature_names', 'procedure', 'medians'])
def test_invalid_optional_artifact_retains_five_specialists(failure, tmp_path, monkeypatch):
    rows = samples()
    before = baseline(rows, monkeypatch)
    path = controlled(tmp_path)
    if failure == 'missing':
        path.unlink()
    elif failure == 'disabled':
        monkeypatch.setenv(focus.ENVIRONMENT_KEY, '')
    elif failure == 'oversize':
        path.write_bytes(b' ' * (focus.MAX_BYTES + 1))
    elif failure == 'malformed':
        path.write_text('[]')
    elif failure == 'nesting':
        path.write_text('[' * 2000 + '0' + ']' * 2000)
    elif failure == 'mode':
        monkeypatch.setenv(focus.MODE_KEY, 'unknown')
    else:
        artifact = json.loads(path.read_text())
        head = artifact['heads'][0]
        if failure == 'scale':
            head['scale'][0] = 0
        elif failure == 'coefficient':
            head['intercept'][0] = float('inf')
        elif failure == 'shape':
            head['weights'].pop()
        elif failure == 'medians':
            head['medians'][0] = True
        elif failure == 'head_count':
            artifact['heads'].pop()
        elif failure == 'rows':
            artifact['training_rows'] = True
        else:
            key, value = {'format': ('format', 'unknown'), 'signature': ('embedding_signature', '0' * 64),
                'taxonomy': ('taxonomy_version', 'unknown'), 'weights': ('weights', [.6, .4]),
                'targets': ('target_names', ['Usage-Reading', 'Usage-At_work']),
                'aggregation': ('aggregation', 'mean_embedding'), 'training_aggregation': ('training_aggregation', 'unknown'),
                'feature_names': ('feature_names', []), 'procedure': ('procedure', 'pca9')}[failure]
            artifact[key] = value
        path.write_text(json.dumps(artifact))
    if failure != 'disabled':
        monkeypatch.setenv(focus.ENVIRONMENT_KEY, str(path))
    assert predictor.predict(rows) == before


def test_provenance_input_bounds_and_disabled_shared(monkeypatch):
    rows = samples()
    rows[-1]['artifact_hashes']['musicnn/model.json'] = 'f' * 64
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)
    model = focus.load(focus.DEFAULT_PATH)
    for values in ([], [[.2] * 200] * 129, [[float('nan')] * 200], [[1e7] * 200], [[.2] * 199]):
        with pytest.raises(ValueError):
            focus.score(model, values)
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, '')
    rows = samples()
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)


def test_default_active_local_override_and_identity(monkeypatch):
    monkeypatch.delenv(focus.MODE_KEY)
    monkeypatch.delenv(focus.ENVIRONMENT_KEY)
    assert focus.mode() == 'active'
    assert focus.configured_path() == str(focus.DEFAULT_PATH)
    active = vibe_preparation.version()
    features.LOCAL_ENV.write_text(focus.MODE_KEY + '=off\n')
    assert focus.mode() == 'off' and vibe_preparation.version() != active
    monkeypatch.setenv(focus.MODE_KEY, 'active')
    assert vibe_preparation.version() == active
    features.LOCAL_ENV.write_text(focus.ENVIRONMENT_KEY + '=\n')
    assert focus.configured_path() is None and vibe_preparation.version() != active


def test_missing_metadata_uses_direct_measured_focus(store):
    store.tracks.upsert_track('bare', 'Bare', '', '', 240)
    key = store.tracks.identity_key_for_video('bare')
    for now, row in enumerate(samples('bare'), 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, key)[focus.VIBE]['score'] == predictor.predict(samples('bare'))['suitability'][focus.VIBE]
    assert fits(store, key)[focus.VIBE]['source'] == 'audio_measurement'
