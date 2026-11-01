"""No-joy export, five-specialist composition and preserved radio inputs."""
from copy import deepcopy
import json

import pytest

from tests.rec.test_vibe_prediction import drain, fits, track
from tests.test_audio_vibe_cruise import samples
from yt_playlist.core import catalog, features
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec import audio_vibe_cruise as cruise, audio_vibe_release as release, audio_vibe_relax as relax
from yt_playlist.rec import audio_vibe_uplift as uplift, audio_vibe_energize as energize
from yt_playlist.rec import audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_preview as preview
from yt_playlist.rec import home_vibe, musical_character, vibe_preparation

SPECIALISTS = (cruise, release, relax, uplift, energize)


@pytest.fixture(autouse=True)
def production_models(isolated_app_home, monkeypatch, tmp_path):
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    monkeypatch.setenv(focus.MODE_KEY, 'off')
    for module in (shared, *SPECIALISTS):
        monkeypatch.setenv(module.ENVIRONMENT_KEY, str(module.DEFAULT_PATH))
    for module in SPECIALISTS:
        monkeypatch.setenv(module.MODE_KEY, 'active')


def baseline(rows, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv(energize.MODE_KEY, 'off')
        return preview.predict(rows, require_complete=False)


def controlled(tmp_path, value=.9):
    artifact = json.loads(energize.DEFAULT_PATH.read_text())
    for expert in artifact['experts'].values():
        expert['model'] = {'kind': 'ridge', 'medians': [0.] * 200, 'mean': [0.] * 200,
                           'scale': [1.] * 200, 'weights': [[0.] for _ in range(200)],
                           'intercept': [value]}
    path = tmp_path / 'energize.json'
    path.write_text(json.dumps(artifact))
    return path


@pytest.mark.parametrize('count', [1, 2, 3])
def test_energize_preserves_four_specialists_classification_and_input(count, monkeypatch):
    rows = samples()[:count]
    untouched = deepcopy(rows)
    before, after = baseline(rows, monkeypatch), preview.predict(rows, require_complete=False)
    assert {k: v for k, v in before.items() if k not in ('suitability', 'individual_models')} == {
        k: v for k, v in after.items() if k not in ('suitability', 'individual_models')}
    assert set(after['individual_models']) == {'cruise', 'release', 'relax', 'uplift', 'energize'}
    for vibe in ('cruise', 'release', 'relax', 'uplift'):
        assert after['individual_models'][vibe] == before['individual_models'][vibe]
    assert after['individual_models']['energize']['sample_count'] == count
    assert after['suitability']['energize'] != before['suitability']['energize']
    for vibe in shared.LABELS:
        if vibe != 'energize':
            assert after['suitability'][vibe] == before['suitability'][vibe]
    assert all(module.apply(rows, after) == after for module in SPECIALISTS)
    assert rows == untouched


def test_clipping_precedes_excerpt_averaging(tmp_path):
    path = controlled(tmp_path, 0.)
    artifact = json.loads(path.read_text())
    for expert in artifact['experts'].values():
        expert['model']['weights'][0] = [1.]
    path.write_text(json.dumps(artifact))
    result = energize.score(energize.load(path), [[2.] + [0.] * 199, [-.5] + [0.] * 199])
    assert all(v == .5 for native in result['native'].values() for v in native.values())
    assert result['score'] == .5  # Averaging embeddings first would produce .75.


@pytest.mark.parametrize('source, expected', [('deam', .125), ('pmemo', .125),
    ('soundtracks-all', .25), ('emotify-artist', .25), ('manymusic-natural', .0625),
    ('manymusic-generated', .0625), ('memo2496-pilot', .125)])
def test_each_fixed_weight_independently(source, expected, tmp_path):
    path = controlled(tmp_path, 0.)
    artifact = json.loads(path.read_text())
    artifact['experts'][source]['model']['intercept'] = [1.]
    path.write_text(json.dumps(artifact))
    assert energize.score(energize.load(path), [[0.] * 200])['score'] == expected


def test_exact_export_has_no_joy_or_exercise_heads():
    header = energize.load(energize.DEFAULT_PATH)['header']
    assert header['source_model_sha256'] == '0801f0919f21f9ca7bcc1ad35d7ec872bc60db0c74227ab9423cfd3e9266660e'
    assert header['source_plan_sha256'] == '0500878ae3cc15b57cb840b3e5c4f0723029c026da7938ef53d0ae523cb35d42'
    assert header['training_aggregation'] == 'mean_embedding_per_annotated_recording'
    assert header['procedure'] == 'consensus_no_joy'
    assert header['experts']['emotify-artist']['target_names'] == ['power']
    assert 'cal500-raw-activity' not in header['experts'] and 'activity_maps' not in header
    assert {s: e['training_rows'] for s, e in header['experts'].items()} == {
        'deam': 1800, 'pmemo': 766, 'soundtracks-all': 470, 'emotify-artist': 400,
        'manymusic-natural': 188, 'manymusic-generated': 95, 'memo2496-pilot': 63}


@pytest.mark.parametrize('failure', ['missing', 'disabled', 'oversize', 'malformed', 'nesting', 'format', 'signature',
    'taxonomy', 'scale', 'coefficient', 'shape', 'mode', 'weights', 'joy', 'source', 'aggregation',
    'training_aggregation', 'rows', 'feature_names', 'procedure'])
def test_optional_failure_preserves_four_specialists_and_shared(failure, tmp_path, monkeypatch):
    rows = samples()
    before = baseline(rows, monkeypatch)
    path = controlled(tmp_path)
    if failure == 'missing':
        path.unlink()
    elif failure == 'disabled':
        monkeypatch.setenv(energize.ENVIRONMENT_KEY, '')
    elif failure == 'oversize':
        path.write_bytes(b' ' * (energize.MAX_BYTES + 1))
    elif failure == 'malformed':
        path.write_text('[]')
    elif failure == 'nesting':
        path.write_text('[' * 2000 + '0' + ']' * 2000)
    elif failure == 'mode':
        monkeypatch.setenv(energize.MODE_KEY, 'unexpected')
    else:
        artifact = json.loads(path.read_text())
        expert = artifact['experts']['emotify-artist']
        if failure == 'scale':
            expert['model']['scale'][0] = 0
        elif failure == 'coefficient':
            expert['model']['intercept'][0] = float('inf')
        elif failure == 'shape':
            expert['model']['weights'].pop()
        elif failure == 'joy':
            expert['target_names'] = ['joyful_activation']
        elif failure == 'source':
            del artifact['experts']['memo2496-pilot']
        elif failure == 'weights':
            artifact['weights']['emotify-artist'] = .125
        elif failure == 'rows':
            expert['training_rows'] = True
        else:
            key, value = {'format': ('format', 'unknown'), 'signature': ('embedding_signature', '0' * 64),
                'taxonomy': ('taxonomy_version', 'unknown'), 'aggregation': ('aggregation', 'mean_embedding'),
                'training_aggregation': ('training_aggregation', 'unknown'), 'feature_names': ('feature_names', []),
                'procedure': ('procedure', 'consensus')}[failure]
            artifact[key] = value
        path.write_text(json.dumps(artifact))
    if failure != 'disabled':
        monkeypatch.setenv(energize.ENVIRONMENT_KEY, str(path))
    assert preview.predict(rows) == before


def test_provenance_excerpt_bounds_and_disabled_shared(monkeypatch):
    rows = samples()
    rows[-1]['artifact_hashes']['musicnn/model.json'] = 'f' * 64
    assert preview.predict(rows) == preview.predict(rows, use_model=False)
    model = energize.load(energize.DEFAULT_PATH)
    for values in ([], [[.2] * 200] * 129, [[float('nan')] * 200], [[1e7] * 200], [[.2] * 199]):
        with pytest.raises(ValueError):
            energize.score(model, values)
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, '')
    rows = samples()
    assert preview.predict(rows) == preview.predict(rows, use_model=False)


def test_default_active_local_rollback_and_identity(monkeypatch):
    monkeypatch.delenv(energize.MODE_KEY)
    monkeypatch.delenv(energize.ENVIRONMENT_KEY)
    assert energize.mode() == 'active'
    assert energize.configured_path() == str(energize.DEFAULT_PATH)
    active = vibe_preparation.version()
    features.LOCAL_ENV.write_text(energize.MODE_KEY + '=off\n')
    assert energize.mode() == 'off' and vibe_preparation.version() != active
    monkeypatch.setenv(energize.MODE_KEY, 'active')
    assert vibe_preparation.version() == active
    features.LOCAL_ENV.write_text(energize.ENVIRONMENT_KEY + '=\n')
    assert energize.configured_path() is None and vibe_preparation.version() != active


def test_bounded_refresh_preserves_intensity_lineage_feedback_and_rollback(store, tmp_path, monkeypatch):
    _tid, own = track(store, 'observed_song')
    _tid, neighbour = track(store, 'neighbour')
    rows = samples()
    monkeypatch.setenv(energize.MODE_KEY, 'off')
    drain(store, batch=1)
    for now, row in enumerate(rows[:2], 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, own)['energize']['source'] == 'heuristic'
    store.audio_vibes.save_sample(rows[2], 3)
    store.audio_vibes.save_feedback(rows[0]['run_id'], [], ['energize'], preview.TAXONOMY_VERSION, None, 4)
    feedback = store.audio_vibes.feedback(rows[0]['run_id'])
    drain(store, batch=1)
    keys = [own, neighbour]
    before = {key: fits(store, key) for key in keys}
    old_profiles = musical_character.load(store, keys)
    monkeypatch.setenv(energize.ENVIRONMENT_KEY, str(controlled(tmp_path)))
    monkeypatch.setenv(energize.MODE_KEY, 'active')
    statements = []
    store.conn.set_trace_callback(statements.append)
    drain(store, batch=1)
    active = musical_character.load(store, keys)
    assert fits(store, own)['energize']['score'] == pytest.approx(.9)
    assert fits(store, neighbour)['energize']['recordings'] == 1
    assert fits(store, neighbour)['energize']['score'] > before[neighbour]['energize']['score']
    for key in keys:
        after = fits(store, key)
        assert set(after) == set(shared.LABELS)
        assert active[key]['vibes']['intensity_fits'] == old_profiles[key]['vibes']['intensity_fits']
        for vibe in shared.LABELS:
            if vibe != 'energize':
                assert after[vibe] == before[key][vibe]
        assert musical_character.intensity_evidence({'musical': active[key]}) == musical_character.intensity_evidence(
            {'musical': old_profiles[key]})
        assert home_vibe.affinity({'key': key}, 'energize', active) == after['energize']['score']
    measured = vibe_preparation.measured({'samples': rows, 'metadata': {}}, [], {}, {})['fits']
    shared_scores = {r['vibe']: r['score'] for r in preview.predict(rows)['ranking']}
    for channel, vibe in INTENSITY_CHANNELS.items():
        assert measured[channel]['score'] == shared_scores[vibe]
    active_stamp = vibe_preparation.version()
    controlled(tmp_path, .3)
    assert vibe_preparation.version() != active_stamp
    drain(store, batch=1)
    assert fits(store, own)['energize']['score'] == pytest.approx(.3)
    for now, row in enumerate(samples(), 10):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, neighbour)['energize']['recordings'] == 1
    history = {r[0] for r in store.conn.execute('SELECT id FROM measurements')}
    monkeypatch.setenv(energize.MODE_KEY, 'off')
    drain(store, batch=1)
    assert history <= {r[0] for r in store.conn.execute('SELECT id FROM measurements')}
    for key in keys:
        assert fits(store, key) == before[key]
    store.conn.set_trace_callback(None)
    assert store.audio_vibes.feedback(rows[0]['run_id']) == feedback
    assert not any('audio_annotation' in sql or 'audio_taxonom' in sql for sql in statements)
    assert not list(store.conn.execute('PRAGMA foreign_key_check'))


def test_missing_metadata_and_explicit_intensity_override(store):
    tid = store.tracks.upsert_track('bare', 'Bare', '', '', 240)
    key = store.tracks.identity_key_for_video('bare')
    for now, row in enumerate(samples('bare'), 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    assert fits(store, key)['energize']['score'] == preview.predict(samples('bare'))['suitability']['energize']
    assert fits(store, key)['energize']['source'] == 'audio_measurement'
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .05, now=4)
    drain(store, batch=1)
    profile = musical_character.load(store, [key])[key]
    assert musical_character.intensity_evidence({'musical': profile}) == (.05, 1)
    assert musical_character.provenance({'musical': profile})['intensity_vibe'] is None
