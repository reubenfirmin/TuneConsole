"""Fixed Relax blend, excerpt ordering, composition and bounded preparation."""
from copy import deepcopy
import json

import pytest

from tests.rec.test_vibe_prediction import drain, fits, individual_model_rollout, track
from tests.test_audio_vibe_cruise import samples
from yt_playlist.core import features
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release, audio_vibe_relax as relax
from yt_playlist.rec import audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor
from yt_playlist.rec import home_vibe, musical_character, vibe_preparation


@pytest.fixture(autouse=True)
def production_models(isolated_app_home, monkeypatch, tmp_path):
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    monkeypatch.setenv(focus.MODE_KEY, 'off')
    for module in (shared, cruise, release, relax):
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
    for module in (cruise, release, relax):
        monkeypatch.setenv(module.MODE_KEY, 'active')


def baseline(rows, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv(relax.MODE_KEY, 'off')
        return predictor.predict(rows, require_complete=False)


def controlled(tmp_path, value=.9):
    artifact = json.loads(relax.DEFAULT_PATH.read_text())
    for expert in artifact['experts'].values():
        targets = expert['target_names']
        width = len(targets)
        expert['model'] = {'kind': 'ridge', 'medians': [0.] * 200, 'mean': [0.] * 200,
                           'scale': [1.] * 200, 'weights': [[0.] * width for _ in range(200)],
                           'intercept': [1 - value if t in ('tension', 'energy', 'arousal') else value for t in targets]}
    path = tmp_path / 'relax.json'
    path.write_text(json.dumps(artifact))
    return path


@pytest.mark.parametrize('count', [1, 2, 3])
def test_relax_preserves_both_specialists_shared_classification_and_input(count, monkeypatch):
    rows = samples()[:count]
    untouched = deepcopy(rows)
    before, after = baseline(rows, monkeypatch), predictor.predict(rows, require_complete=False)
    assert {k: v for k, v in before.items() if k not in ('suitability', 'individual_models')} == {
        k: v for k, v in after.items() if k not in ('suitability', 'individual_models')}
    for vibe in ('cruise', 'release'):
        assert after['individual_models'][vibe] == before['individual_models'][vibe]
    assert after['individual_models']['relax']['sample_count'] == count
    assert after['suitability']['relax'] != before['suitability']['relax']
    for vibe in shared.LABELS:
        if vibe != 'relax':
            assert after['suitability'][vibe] == before['suitability'][vibe]
    assert cruise.apply(rows, after) == release.apply(rows, after) == after
    assert rows == untouched


def test_each_native_output_is_clipped_before_excerpt_averaging(tmp_path):
    path = controlled(tmp_path, .5)
    artifact = json.loads(path.read_text())
    for expert in artifact['experts'].values():
        width = len(expert['target_names'])
        expert['model']['intercept'] = [0.] * width
        expert['model']['weights'][0] = [1.] * width
    path.write_text(json.dumps(artifact))
    result = relax.score(relax.load(path), [[2.] + [0.] * 199, [-.5] + [0.] * 199])
    assert all(v == .5 for native in result['native'].values() for v in native.values())
    assert result['score'] == .5  # Embedding-first clipping produces different Soundtracks suitability.


def test_artifact_is_exact_fixed_candidate_with_original_training_provenance():
    header = relax.load(relax.DEFAULT_PATH)['header']
    assert header['source_model_sha256'] == '46ca8cc085adaab92eed16fa51b49ac4de311996a8b9492f144a5cf70aec7851'
    assert header['weights'] == {'emotify-artist': .5, 'soundtracks-all': .25,
                                 'manymusic-natural': .0625, 'manymusic-generated': .0625, 'memo2496-pilot': .125}
    assert {s: e['training_rows'] for s, e in header['experts'].items()} == {
        'emotify-artist': 400, 'soundtracks-all': 470, 'manymusic-natural': 188,
        'manymusic-generated': 95, 'memo2496-pilot': 63}


@pytest.mark.parametrize('failure', ['missing', 'disabled', 'oversize', 'malformed', 'nesting', 'format', 'signature',
                                    'taxonomy', 'scale', 'coefficient', 'shape', 'mode', 'weights', 'target', 'source', 'aggregation'])
def test_optional_failure_preserves_both_specialists_and_shared(failure, tmp_path, monkeypatch):
    rows = samples()
    before = baseline(rows, monkeypatch)
    path = controlled(tmp_path)
    if failure == 'missing':
        path.unlink()
    elif failure == 'disabled':
        monkeypatch.setenv(relax.ENVIRONMENT_KEY, '')
    elif failure == 'oversize':
        path.write_bytes(b' ' * (relax.MAX_BYTES + 1))
    elif failure == 'malformed':
        path.write_text('[]')
    elif failure == 'nesting':
        path.write_text('[' * 2000 + '0' + ']' * 2000)
    elif failure == 'mode':
        monkeypatch.setenv(relax.MODE_KEY, 'unexpected')
    else:
        artifact = json.loads(path.read_text())
        expert = artifact['experts']['emotify-artist']
        if failure == 'scale':
            expert['model']['scale'][0] = 0
        elif failure == 'coefficient':
            expert['model']['intercept'][0] = float('inf')
        elif failure == 'shape':
            expert['model']['weights'].pop()
        elif failure == 'target':
            expert['target_names'].reverse()
        elif failure == 'source':
            del artifact['experts']['memo2496-pilot']
        elif failure == 'weights':
            artifact['weights']['memo2496-pilot'] = 0.
        else:
            key, value = {'format': ('format', 'unknown'), 'signature': ('embedding_signature', '0' * 64),
                          'taxonomy': ('taxonomy_version', 'unknown'), 'aggregation': ('aggregation', 'mean_embedding')}[failure]
            artifact[key] = value
        path.write_text(json.dumps(artifact))
    if failure != 'disabled':
        monkeypatch.setenv(relax.ENVIRONMENT_KEY, str(path))
    assert predictor.predict(rows) == before


def test_provenance_and_excerpt_bounds(monkeypatch):
    rows = samples()
    rows[-1]['artifact_hashes']['musicnn/model.json'] = 'f' * 64
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)
    model = relax.load(relax.DEFAULT_PATH)
    for values in ([], [[.2] * 200] * 129, [[float('nan')] * 200], [[1e7] * 200], [[.2] * 199]):
        with pytest.raises(ValueError):
            relax.score(model, values)
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, '')
    rows = samples()
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)


def test_default_active_and_local_rollback_change_preparation_identity(monkeypatch):
    monkeypatch.delenv(relax.MODE_KEY)
    monkeypatch.delenv(relax.ENVIRONMENT_KEY)
    assert relax.mode() == 'active'
    assert relax.configured_path() == str(relax.DEFAULT_PATH)
    active = vibe_preparation.version()
    features.LOCAL_ENV.write_text(relax.MODE_KEY + '=off\n')
    assert relax.mode() == 'off'
    assert vibe_preparation.version() != active
    monkeypatch.setenv(relax.MODE_KEY, 'active')
    assert vibe_preparation.version() == active
    features.LOCAL_ENV.write_text(relax.ENVIRONMENT_KEY + '=\n')
    assert relax.configured_path() is None
    assert vibe_preparation.version() != active


def test_bounded_rollout_propagates_one_recording_preserves_history_and_rolls_back(
        store, tmp_path, monkeypatch, individual_model_rollout):
    _tid, own = track(store, 'observed_song')
    _tid, neighbour = track(store, 'neighbour')
    rows = samples()
    monkeypatch.setenv(relax.MODE_KEY, 'off')
    drain(store, batch=1)
    for now, row in enumerate(rows[:2], 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, own)['relax']['source'] == 'heuristic'
    store.audio_vibes.save_sample(rows[2], 3)
    store.audio_vibes.save_feedback(rows[0]['run_id'], [], ['relax'], predictor.TAXONOMY_VERSION, None, 4)
    feedback = store.audio_vibes.feedback(rows[0]['run_id'])
    drain(store, batch=1)
    keys = [own, neighbour]
    before = {key: fits(store, key) for key in keys}
    old_profiles = musical_character.load(store, keys)
    monkeypatch.setenv(relax.ENVIRONMENT_KEY, str(controlled(tmp_path)))
    monkeypatch.setenv(relax.MODE_KEY, 'active')
    statements = []
    store.conn.set_trace_callback(statements.append)
    drain(store, batch=1)
    active = musical_character.load(store, keys)
    assert fits(store, own)['relax']['score'] == pytest.approx(.9)
    assert fits(store, neighbour)['relax']['recordings'] == 1
    assert fits(store, neighbour)['relax']['score'] > before[neighbour]['relax']['score']
    for key in keys:
        after = fits(store, key)
        for vibe in shared.LABELS:
            if vibe != 'relax':
                assert after[vibe] == before[key][vibe]
        assert musical_character.intensity_evidence({'musical': active[key]}) == musical_character.intensity_evidence(
            {'musical': old_profiles[key]})
        assert home_vibe.affinity({'key': key}, 'relax', active) == after['relax']['score']
    active_stamp = vibe_preparation.version()
    controlled(tmp_path, .3)
    assert vibe_preparation.version() != active_stamp
    drain(store, batch=1)
    assert fits(store, own)['relax']['score'] == pytest.approx(.3)
    for now, row in enumerate(samples(), 10):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, neighbour)['relax']['recordings'] == 1
    history = {r[0] for r in store.conn.execute('SELECT id FROM measurements')}
    monkeypatch.setenv(relax.MODE_KEY, 'off')
    drain(store, batch=1)
    assert history <= {r[0] for r in store.conn.execute('SELECT id FROM measurements')}
    for key in keys:
        assert fits(store, key) == before[key]
    store.conn.set_trace_callback(None)
    assert store.audio_vibes.feedback(rows[0]['run_id']) == feedback
    assert not any('audio_annotation' in sql or 'audio_taxonom' in sql for sql in statements)
    assert not list(store.conn.execute('PRAGMA foreign_key_check'))


def test_missing_metadata_still_uses_relax(store):
    store.tracks.upsert_track('bare', 'Bare', '', '', 240)
    key = store.tracks.identity_key_for_video('bare')
    for now, row in enumerate(samples('bare'), 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, key)['relax']['score'] == predictor.predict(samples('bare'))['suitability']['relax']
    assert fits(store, key)['relax']['source'] == 'audio_measurement'
