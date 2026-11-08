"""Release routing, independent score scales and durable preparation."""
from copy import deepcopy
import json

import pytest

from tests.rec.test_vibe_prediction import drain, fits, track
from tests.test_audio_vibe_cruise import samples
from yt_playlist.core import catalog, features
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release
from yt_playlist.rec import audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor
from yt_playlist.rec import musical_character, vibe_preparation


@pytest.fixture(autouse=True)
def production_models(isolated_app_home, monkeypatch, tmp_path):
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    monkeypatch.setenv(focus.MODE_KEY, 'off')
    for module in (shared, cruise, release):
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
    monkeypatch.setenv(cruise.MODE_KEY, 'active')
    monkeypatch.setenv(release.MODE_KEY, 'active')


def baseline(rows, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv(release.MODE_KEY, 'off')
        return predictor.predict(rows, require_complete=False)


def controlled(tmp_path, value=.9):
    artifact = json.loads(release.DEFAULT_PATH.read_text())
    artifact['model'] = {'kind': 'ridge', 'medians': [0.] * 200, 'mean': [0.] * 200,
                         'scale': [1.] * 200, 'weights': [[0.] for _ in range(200)], 'intercept': [value]}
    path = tmp_path / 'release.json'
    path.write_text(json.dumps(artifact))
    return path


@pytest.mark.parametrize('count', [1, 2, 3])
def test_release_preserves_cruise_shared_classification_and_input(count, monkeypatch):
    rows = samples()[:count]
    original = deepcopy(rows)
    before, after = baseline(rows, monkeypatch), predictor.predict(rows, require_complete=False)
    assert {k: v for k, v in before.items() if k not in ('suitability', 'individual_models')} == {
        k: v for k, v in after.items() if k not in ('suitability', 'individual_models')}
    assert after['individual_models']['cruise'] == before['individual_models']['cruise']
    assert after['individual_models']['release']['sample_count'] == count
    assert after['individual_models']['release']['score_semantics'] == 'independent_uncalibrated_suitability'
    assert after['suitability']['release'] != before['suitability']['release']
    for vibe in shared.LABELS:
        if vibe != 'release':
            assert after['suitability'][vibe] == before['suitability'][vibe]
    assert cruise.apply(rows, after) == after  # Composition cannot erase Release.
    assert rows == original


def test_clip_each_excerpt_before_averaging(tmp_path):
    path = controlled(tmp_path, 0.)
    artifact = json.loads(path.read_text())
    artifact['model']['weights'][0] = [1.]
    path.write_text(json.dumps(artifact))
    result = release.score(release.load(path), [[2.] + [0.] * 199, [-.5] + [0.] * 199])
    assert result['score'] == .5  # Averaging embeddings before clipping would give .75.


def test_selected_artifact_is_the_original_aggression_candidate():
    model = release.load(release.DEFAULT_PATH)
    assert model['sha256'] == '3155a3a5dd44776f950688f643eff4fae9ded38029dea9af7c34eead634cc0ac'
    header = model['header']
    assert header['source_model_sha256'] == '4591b47b47775fe095a5260feda4e9819abd61b9b663834595fa6c9bffebe415'
    assert header['training_rows'] == 187
    assert header['model']['alpha'] == 1000.


@pytest.mark.parametrize('failure', ['missing', 'disabled', 'oversize', 'malformed', 'nesting', 'format', 'signature',
                                    'taxonomy', 'scale', 'coefficient', 'shape', 'mode'])
def test_optional_failure_preserves_cruise_and_shared(failure, tmp_path, monkeypatch):
    rows = samples()
    before = baseline(rows, monkeypatch)
    path = controlled(tmp_path)
    if failure == 'missing':
        path.unlink()
    elif failure == 'disabled':
        monkeypatch.setenv(release.ENVIRONMENT_KEY, '')
    elif failure == 'oversize':
        path.write_bytes(b' ' * (release.MAX_BYTES + 1))
    elif failure == 'malformed':
        path.write_text('[]')
    elif failure == 'nesting':
        path.write_text('[' * 2000 + '0' + ']' * 2000)
    elif failure == 'mode':
        monkeypatch.setenv(release.MODE_KEY, 'unexpected')
    else:
        artifact = json.loads(path.read_text())
        if failure == 'scale':
            artifact['model']['scale'][0] = 0
        elif failure == 'coefficient':
            artifact['model']['intercept'] = [float('inf')]
        elif failure == 'shape':
            artifact['model']['weights'].pop()
        else:
            key, value = {'format': ('format', 'unknown'), 'signature': ('embedding_signature', '0' * 64),
                          'taxonomy': ('taxonomy_version', 'unknown')}[failure]
            artifact[key] = value
        path.write_text(json.dumps(artifact))
    if failure != 'disabled':
        monkeypatch.setenv(release.ENVIRONMENT_KEY, str(path))
    assert predictor.predict(rows) == before


def test_provenance_and_excerpt_bounds(monkeypatch):
    rows = samples()
    rows[-1]['artifact_hashes']['musicnn/model.json'] = 'f' * 64
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)
    model = release.load(release.DEFAULT_PATH)
    for values in ([], [[.2] * 200] * 129, [[float('nan')] * 200], [[1e7] * 200], [[.2] * 199]):
        with pytest.raises(ValueError):
            release.score(model, values)
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, '')
    rows = samples()
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)


def test_default_active_local_rollback_and_identity(monkeypatch):
    monkeypatch.delenv(release.MODE_KEY)
    monkeypatch.delenv(release.ENVIRONMENT_KEY)
    assert release.mode() == 'active'
    assert release.configured_path() == str(release.DEFAULT_PATH)
    active = vibe_preparation.version()
    features.LOCAL_ENV.write_text(release.MODE_KEY + '=off\n')
    assert release.mode() == 'off'
    assert vibe_preparation.version() != active
    monkeypatch.setenv(release.MODE_KEY, 'active')
    assert vibe_preparation.version() == active
    features.LOCAL_ENV.write_text(release.ENVIRONMENT_KEY + '=\n')
    assert release.configured_path() is None
    assert vibe_preparation.version() != active


def test_missing_metadata_still_uses_release(store):
    store.tracks.upsert_track('bare', 'Bare', '', '', 240)
    key = store.tracks.identity_key_for_video('bare')
    for now, row in enumerate(samples('bare'), 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, key)['release']['score'] == predictor.predict(samples('bare'))['suitability']['release']
    assert fits(store, key)['release']['source'] == 'audio_measurement'


def test_high_release_does_not_replace_classification_or_explicit_intensity(store, tmp_path, monkeypatch):
    tid, key = track(store, 'observed_song')
    rows = samples()
    before = baseline(rows, monkeypatch)
    monkeypatch.setenv(release.ENVIRONMENT_KEY, str(controlled(tmp_path, .99)))
    after = predictor.predict(rows)
    assert after['suitability']['release'] == .99
    for name in ('ranking', 'vibe', 'vibes', 'selection_method', 'score_margin'):
        assert after[name] == before[name]
    for now, row in enumerate(rows, 1):
        store.audio_vibes.save_sample(row, now)
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .05, now=4)
    drain(store, batch=1)
    profile = musical_character.load(store, [key])[key]
    assert profile['vibes']['fits']['release']['score'] == .99
    assert musical_character.intensity_evidence({'musical': profile}) == (.05, 1)
    assert musical_character.provenance({'musical': profile})['intensity_vibe'] is None
