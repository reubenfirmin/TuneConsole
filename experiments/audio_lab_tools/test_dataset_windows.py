"""Guard held-out audio intervals, target-blind selection and feature integrity."""
import copy
import json

import numpy as np
import pytest

from experiments.audio_lab_tools import dataset_windows as windows
from experiments.audio_lab_tools.new_corpus_campaign import checkpoint_finished
from experiments.audio_lab_tools.external_experiment import digest, save


def many_row(start, target, count):
    return {'id': f'manymusic:one:{start}:30', 'recording_id': 'manymusic:one',
        'annotation_start': start, 'annotation_seconds': 30,
        'target_names': ['arousal', 'valence'], 'targets': [target, -target],
        'synthetic_audio': False, 'rater_count': count}


def test_pilot_selection_ignores_labels_listener_count_and_input_order():
    rows = [many_row(0, -1, 100), many_row(40, 1, 1), many_row(80, .5, 8)]
    first = windows.many_music_rows(rows)[0]
    altered = [{**row, 'targets': [-value for value in row['targets']], 'rater_count': 1000 - row['rater_count']}
               for row in reversed(rows)]
    second = windows.many_music_rows(altered)[0]
    assert first['id'] == second['id']
    assert first['chunk_starts'] == second['chunk_starts']
    assert first['signed_native_targets'] == list(reversed(next(row for row in rows if row['id'] == first['id'])['targets']))
    assert first['chunk_starts'][2] + 10 == first['covered_interval'][1]


def trajectory():
    return {name: [[second, -1 if name == 'valence' else 1] for second in range(76)]
            for name in windows.TARGETS}


def test_memo_interval_excludes_opening_and_endpoint_and_rejects_gaps():
    values = trajectory()
    for series in values.values():
        for index in [*range(15), 75]:
            series[index][1] = 0
    assert windows.trajectory_mean(values, 15, 75) == [0., 1.]
    missing = copy.deepcopy(values)
    del missing['arousal'][40]
    with pytest.raises(ValueError, match='Incomplete annotation'):
        windows.trajectory_mean(missing, 15, 75)
    duplicate = copy.deepcopy(values)
    duplicate['valence'].append([40, 0])
    with pytest.raises(ValueError, match='Duplicate trajectory'):
        windows.trajectory_mean(duplicate, 15, 75)
    malformed = copy.deepcopy(values)
    malformed['valence'][40][1] = float('nan')
    with pytest.raises(ValueError, match='Invalid trajectory'):
        windows.trajectory_mean(malformed, 15, 75)


def test_short_audio_or_chunk_gap_never_becomes_padded_music():
    row = {'covered_interval': [15, 75], 'chunk_starts': list(range(15, 75, 10))}
    windows.check_interval(row, 75)
    with pytest.raises(ValueError, match='complete consecutive'):
        windows.check_interval(row, 74.99)
    with pytest.raises(ValueError, match='complete consecutive'):
        windows.check_interval({**row, 'chunk_starts': [15, 25, 45, 55, 65]}, 80)


@pytest.mark.parametrize('start', [44.33333, 113.3333, 3.666667])
def test_released_decimal_intervals_accept_rounding_but_reject_real_gaps(start):
    row = {'covered_interval': [start, start + 30],
           'chunk_starts': [start + offset for offset in (0, 10, 20)]}
    windows.check_interval(row, 300)
    with pytest.raises(ValueError, match='complete consecutive'):
        windows.check_interval(row, row['covered_interval'][1] - 1e-9)
    gap = copy.deepcopy(row)
    gap['chunk_starts'][1] += 1e-6
    with pytest.raises(ValueError, match='complete consecutive'):
        windows.check_interval(gap, 300)
    with pytest.raises(ValueError, match='complete consecutive'):
        windows.check_interval({**row, 'chunk_starts': [start, float('nan'), start + 20]}, 300)


def test_reused_features_require_original_digest_and_identical_runtime(tmp_path):
    provenance = {'plan_sha256': 'new-plan', 'script_sha256': 'new-script', 'decoder': 'ffmpeg-one'}
    expected = {'audio_sha256': 'audio', 'start': 44.33333, 'seconds': 10, 'provenance': provenance}
    result = {'research_backend': 'tensorflow', 'sample_count': 160000,
              'prediction': {'embedding': {'values': [0.] * 200}}}
    original = {'input': {**expected, 'provenance': {**provenance, 'plan_sha256': 'old-plan',
                                                   'script_sha256': 'old-script'}},
                'result': result, 'result_sha256': windows.content_hash(result)}
    source, reused = tmp_path / 'original.json', tmp_path / 'reused.json'
    save(source, original)
    record = {**original, 'input': expected, 'reused_from': {'path': str(source), 'sha256': digest(source)}}
    save(reused, record)
    assert windows.checked_record(reused, expected)['result'] == result
    original['input']['provenance']['decoder'] = 'ffmpeg-two'
    save(source, original)
    with pytest.raises(ValueError, match='Original reused chunk changed'):
        windows.checked_record(reused, expected)
    record['reused_from']['sha256'] = digest(source)
    save(reused, record)
    with pytest.raises(ValueError, match='runtime, input or result differs'):
        windows.checked_record(reused, expected)


def test_queued_pilot_waits_for_same_invocation_to_finish():
    assert not checkpoint_finished({'started_at': 'one', 'state': 'failed', 'active': 'deam'}, 'one')
    assert not checkpoint_finished({'started_at': 'one', 'state': 'running', 'active': 'deam'}, 'one')
    assert checkpoint_finished({'started_at': 'one', 'state': 'partial', 'active': None}, 'one')
    with pytest.raises(ValueError, match='invocation changed'):
        checkpoint_finished({'started_at': 'two', 'state': 'complete', 'active': None}, 'one')


def test_known_jamendo_overlap_survives_different_audio_encodings(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, 'OUT', tmp_path)
    save(tmp_path / 'jamendo-human-4shards.features.json', {'rows': [
        {'id': 'track_7', 'source_artist_id': 'artist_11', 'audio_sha256': 'old-encoding'}]})
    targets = [
        {'id': 'recording-overlap', 'recording_id': 'manymusic:7', 'artist_id': '12',
         'subset': 'manymusic-natural', 'audio_sha256': 'new-encoding'},
        {'id': 'artist-overlap', 'recording_id': 'manymusic:8', 'artist_id': '11',
         'subset': 'manymusic-natural', 'audio_sha256': 'third-encoding'},
        {'id': 'unseen', 'recording_id': 'manymusic:9', 'artist_id': '13',
         'subset': 'manymusic-natural', 'audio_sha256': 'fourth-encoding'}]
    found = windows.overlap_audit(targets)['known_corpus_overlaps']
    assert found['recording-overlap'][0]['match'] == 'jamendo_recording_id'
    assert found['artist-overlap'][0]['match'] == 'jamendo_artist_id'
    assert 'unseen' not in found


def test_overlap_audit_binds_known_collaborators_without_guessing_band_splits(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, 'OUT', tmp_path)
    save(tmp_path / 'prior.features.json', {'rows': [
        {'id': 'one', 'artist': 'First+Second', 'audio_sha256': 'source-one'},
        {'id': 'two', 'artist': 'Simon & Garfunkel', 'audio_sha256': 'source-two'},
        {'id': 'three', 'artist': 'Various Artists', 'audio_sha256': 'source-three'}]})
    targets = [{'id': key, 'artist': name, 'audio_sha256': key} for key, name in
               [('collaborator', 'SECOND'), ('band-member', 'Simon'), ('different', 'Third'),
                ('generic', 'Various Artists')]]
    audit = windows.overlap_audit(targets)
    found = audit['known_corpus_overlaps']
    assert found['collaborator'][0]['match'] == 'normalized_artist_credit'
    assert found['collaborator'][0]['credit'] == 'second'
    assert 'band-member' not in found
    assert 'different' not in found
    assert 'generic' not in found
    assert 'generic' in audit['unresolved_artist_credits']


def test_target_loading_requires_all_chunks_and_unchanged_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, 'WORK', tmp_path)
    row = {'id': 'one', 'audio_sha256': 'audio', 'covered_interval': [0, 20], 'chunk_starts': [0, 10]}
    monkeypatch.setattr(windows, 'load_plan', lambda: ({'rows': [row]}, 'frozen-plan'))
    provenance = {'plan_sha256': 'frozen-plan'}
    chunks = []
    for start in row['chunk_starts']:
        result = {'research_backend': 'tensorflow', 'sample_count': 160000,
            'prediction': {'model': 'msd-musicnn-1', 'model_version': '1',
                'bundle': {'files': [{'path': name, 'sha256': 'a' * 64} for name in
                                     ('musicnn/model.json', 'musicnn/group1-shard1of1.bin')]},
                'embedding': {'version': 'musicnn-embedding-1', 'layer': 'model/dense/BiasAdd',
                    'graph_policy': 'restore-fused-preactivation-1', 'pooling': 'mean_full_patches',
                    'patches': 3, 'values': [float(start)] * 200}}}
        path = tmp_path / f'{start}.json'
        save(path, {'input': {'audio_sha256': 'audio', 'start': start, 'seconds': 10, 'provenance': provenance},
                    'result': result, 'result_sha256': windows.content_hash(result)})
        chunks.append({'start': start, 'seconds': 10, 'path': str(path), 'sha256': digest(path)})
    complete = {'state': 'complete', 'provenance': provenance, 'rows': [{**row, 'duration': 20, 'chunks': chunks}], 'errors': []}
    path = tmp_path / 'features.json'
    save(path, complete)
    rows, vectors, _ = windows.target_features()
    assert np.array_equal(vectors['one'].mean(0), np.full(200, 5.))
    missing = copy.deepcopy(complete)
    missing['rows'][0]['chunks'].pop()
    save(path, missing)
    with pytest.raises(ValueError, match='ordered chunk'):
        windows.target_features()
    save(path, complete)
    (tmp_path / '0.json').write_text(json.dumps({'changed': True}))
    with pytest.raises(ValueError, match='chunk file changed'):
        windows.target_features()
