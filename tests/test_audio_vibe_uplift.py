"""Fixed Uplift export, four-specialist composition and bounded rollout."""
from copy import deepcopy
import json

import pytest

from tests.rec.test_vibe_prediction import drain, fits, track
from tests.test_audio_vibe_cruise import samples
from yt_playlist.core import features
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release, audio_vibe_relax as relax
from yt_playlist.rec import audio_vibe_uplift as uplift, audio_vibe_model as shared, audio_vibe_preview as preview
from yt_playlist.rec import audio_vibe_energize as energize
from yt_playlist.rec import audio_vibe_focus as focus
from yt_playlist.rec import home_vibe, musical_character, vibe_preparation


@pytest.fixture(autouse=True)
def production_models(isolated_app_home, monkeypatch, tmp_path):
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    monkeypatch.setenv(energize.MODE_KEY, 'off')
    monkeypatch.setenv(focus.MODE_KEY, 'off')
    for module in (shared, cruise, release, relax, uplift):
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
    for module in (cruise, release, relax, uplift):
        monkeypatch.setenv(module.MODE_KEY, 'active')


def baseline(rows, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv(uplift.MODE_KEY, 'off')
        return preview.predict(rows, require_complete=False)


def controlled(tmp_path, value=.9):
    artifact = json.loads(uplift.DEFAULT_PATH.read_text())
    for expert in artifact['experts'].values():
        width = len(expert['target_names'])
        expert['model'] = {'kind': 'ridge', 'medians': [0.] * 200, 'mean': [0.] * 200,
                           'scale': [1.] * 200, 'weights': [[0.] * width for _ in range(200)],
                           'intercept': [value] * width}
    path = tmp_path / 'uplift.json'
    path.write_text(json.dumps(artifact))
    return path


@pytest.mark.parametrize('count', [1, 2, 3])
def test_uplift_preserves_three_specialists_shared_classification_and_input(count, monkeypatch):
    rows = samples()[:count]
    untouched = deepcopy(rows)
    before, after = baseline(rows, monkeypatch), preview.predict(rows, require_complete=False)
    assert {k: v for k, v in before.items() if k not in ('suitability', 'individual_models')} == {
        k: v for k, v in after.items() if k not in ('suitability', 'individual_models')}
    assert set(after['individual_models']) == {'cruise', 'release', 'relax', 'uplift'}
    for vibe in ('cruise', 'release', 'relax'):
        assert after['individual_models'][vibe] == before['individual_models'][vibe]
    assert after['individual_models']['uplift']['sample_count'] == count
    assert after['suitability']['uplift'] != before['suitability']['uplift']
    for vibe in shared.LABELS:
        if vibe != 'uplift':
            assert after['suitability'][vibe] == before['suitability'][vibe]
    assert cruise.apply(rows, after) == release.apply(rows, after) == relax.apply(rows, after) == after
    assert rows == untouched


def test_clipping_precedes_excerpt_averaging(tmp_path):
    path = controlled(tmp_path, 0.)
    artifact = json.loads(path.read_text())
    for expert in artifact['experts'].values():
        expert['model']['weights'][0] = [1.] * len(expert['target_names'])
    path.write_text(json.dumps(artifact))
    result = uplift.score(uplift.load(path), [[2.] + [0.] * 199, [-.5] + [0.] * 199])
    assert all(v == .5 for native in result['native'].values() for v in native.values())
    assert result['score'] == .5  # Averaging embeddings first would produce .75.


def test_soundtracks_map_and_manymusic_equal_strata_use_fixed_weights(tmp_path):
    path = controlled(tmp_path, 0.)
    artifact = json.loads(path.read_text())
    artifact['experts']['soundtracks-all']['model']['intercept'] = [1., 0.]
    artifact['experts']['manymusic-natural']['model']['intercept'] = [1.]
    path.write_text(json.dumps(artifact))
    result = uplift.score(uplift.load(path), [[0.] * 200])
    assert result['source_scores']['soundtracks-all'] == .5
    assert result['score'] == .25 * .5 + .0625


def test_exact_selected_artifact_and_training_provenance():
    header = uplift.load(uplift.DEFAULT_PATH)['header']
    assert header['source_model_sha256'] == 'a1f1087b75f844eef74be62b39d627df611be9c2edabdc99920cbb591342f737'
    assert header['source_plan_sha256'] == '2bf73a9f8f1360f50173f2cb3311dd1f31f9584a0d51054749704e5f28bb5b66'
    assert header['training_aggregation'] == 'mean_embedding_per_annotated_recording'
    assert header['weights'] == {'deam': .125, 'pmemo': .125, 'soundtracks-all': .25,
        'jamendo-human-4shards': .125, 'emotify-artist': .125, 'manymusic-natural': .0625,
        'manymusic-generated': .0625, 'memo2496-pilot': .125}
    assert 'learned_weights' not in header
    assert {s: e['training_rows'] for s, e in header['experts'].items()} == {
        'deam': 1800, 'pmemo': 766, 'soundtracks-all': 470, 'jamendo-human-4shards': 216,
        'emotify-artist': 400, 'manymusic-natural': 188, 'manymusic-generated': 95, 'memo2496-pilot': 63}


@pytest.mark.parametrize('failure', ['missing', 'disabled', 'oversize', 'malformed', 'nesting', 'format', 'signature',
                                    'taxonomy', 'scale', 'coefficient', 'shape', 'mode', 'weights', 'target', 'source',
                                    'aggregation', 'training_aggregation', 'rows', 'feature_names'])
def test_optional_failure_preserves_three_specialists_and_shared(failure, tmp_path, monkeypatch):
    rows = samples()
    before = baseline(rows, monkeypatch)
    path = controlled(tmp_path)
    if failure == 'missing':
        path.unlink()
    elif failure == 'disabled':
        monkeypatch.setenv(uplift.ENVIRONMENT_KEY, '')
    elif failure == 'oversize':
        path.write_bytes(b' ' * (uplift.MAX_BYTES + 1))
    elif failure == 'malformed':
        path.write_text('[]')
    elif failure == 'nesting':
        path.write_text('[' * 2000 + '0' + ']' * 2000)
    elif failure == 'mode':
        monkeypatch.setenv(uplift.MODE_KEY, 'unexpected')
    else:
        artifact = json.loads(path.read_text())
        expert = artifact['experts']['soundtracks-all']
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
        elif failure == 'rows':
            expert['training_rows'] = True
        else:
            key, value = {'format': ('format', 'unknown'), 'signature': ('embedding_signature', '0' * 64),
                'taxonomy': ('taxonomy_version', 'unknown'), 'aggregation': ('aggregation', 'mean_embedding'),
                'training_aggregation': ('training_aggregation', 'unknown'), 'feature_names': ('feature_names', [])}[failure]
            artifact[key] = value
        path.write_text(json.dumps(artifact))
    if failure != 'disabled':
        monkeypatch.setenv(uplift.ENVIRONMENT_KEY, str(path))
    assert preview.predict(rows) == before


def test_provenance_excerpt_bounds_and_disabled_shared(monkeypatch):
    rows = samples()
    rows[-1]['artifact_hashes']['musicnn/model.json'] = 'f' * 64
    assert preview.predict(rows) == preview.predict(rows, use_model=False)
    model = uplift.load(uplift.DEFAULT_PATH)
    for values in ([], [[.2] * 200] * 129, [[float('nan')] * 200], [[1e7] * 200], [[.2] * 199]):
        with pytest.raises(ValueError):
            uplift.score(model, values)
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, '')
    rows = samples()
    assert preview.predict(rows) == preview.predict(rows, use_model=False)


def test_default_active_local_rollback_and_identity(monkeypatch):
    monkeypatch.delenv(uplift.MODE_KEY)
    monkeypatch.delenv(uplift.ENVIRONMENT_KEY)
    assert uplift.mode() == 'active'
    assert uplift.configured_path() == str(uplift.DEFAULT_PATH)
    active = vibe_preparation.version()
    features.LOCAL_ENV.write_text(uplift.MODE_KEY + '=off\n')
    assert uplift.mode() == 'off'
    assert vibe_preparation.version() != active
    monkeypatch.setenv(uplift.MODE_KEY, 'active')
    assert vibe_preparation.version() == active
    features.LOCAL_ENV.write_text(uplift.ENVIRONMENT_KEY + '=\n')
    assert uplift.configured_path() is None
    assert vibe_preparation.version() != active


def test_bounded_rollout_updates_one_vote_preserves_history_feedback_and_rolls_back(store, tmp_path, monkeypatch):
    _tid, own = track(store, 'observed_song')
    _tid, neighbour = track(store, 'neighbour')
    rows = samples()
    monkeypatch.setenv(uplift.MODE_KEY, 'off')
    drain(store, batch=1)
    for now, row in enumerate(rows[:2], 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, own)['uplift']['source'] == 'heuristic'
    store.audio_vibes.save_sample(rows[2], 3)
    store.audio_vibes.save_feedback(rows[0]['run_id'], [], ['uplift'], preview.TAXONOMY_VERSION, None, 4)
    feedback = store.audio_vibes.feedback(rows[0]['run_id'])
    drain(store, batch=1)
    keys = [own, neighbour]
    before = {key: fits(store, key) for key in keys}
    old_profiles = musical_character.load(store, keys)
    monkeypatch.setenv(uplift.ENVIRONMENT_KEY, str(controlled(tmp_path)))
    monkeypatch.setenv(uplift.MODE_KEY, 'active')
    statements = []
    store.conn.set_trace_callback(statements.append)
    drain(store, batch=1)
    active = musical_character.load(store, keys)
    assert fits(store, own)['uplift']['score'] == pytest.approx(.9)
    assert fits(store, neighbour)['uplift']['recordings'] == 1
    assert fits(store, neighbour)['uplift']['score'] > before[neighbour]['uplift']['score']
    for key in keys:
        after = fits(store, key)
        for vibe in shared.LABELS:
            if vibe != 'uplift':
                assert after[vibe] == before[key][vibe]
        assert musical_character.intensity_evidence({'musical': active[key]}) == musical_character.intensity_evidence(
            {'musical': old_profiles[key]})
        assert home_vibe.affinity({'key': key}, 'uplift', active) == after['uplift']['score']
    active_stamp = vibe_preparation.version()
    controlled(tmp_path, .3)
    assert vibe_preparation.version() != active_stamp
    drain(store, batch=1)
    assert fits(store, own)['uplift']['score'] == pytest.approx(.3)
    for now, row in enumerate(samples(), 10):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, neighbour)['uplift']['recordings'] == 1
    history = {r[0] for r in store.conn.execute('SELECT id FROM measurements')}
    monkeypatch.setenv(uplift.MODE_KEY, 'off')
    drain(store, batch=1)
    assert history <= {r[0] for r in store.conn.execute('SELECT id FROM measurements')}
    for key in keys:
        assert fits(store, key) == before[key]
    store.conn.set_trace_callback(None)
    assert store.audio_vibes.feedback(rows[0]['run_id']) == feedback
    assert not any('audio_annotation' in sql or 'audio_taxonom' in sql for sql in statements)
    assert not list(store.conn.execute('PRAGMA foreign_key_check'))


def test_missing_metadata_still_uses_uplift(store):
    store.tracks.upsert_track('bare', 'Bare', '', '', 240)
    key = store.tracks.identity_key_for_video('bare')
    for now, row in enumerate(samples('bare'), 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, key)['uplift']['score'] == preview.predict(samples('bare'))['suitability']['uplift']
    assert fits(store, key)['uplift']['source'] == 'audio_measurement'
