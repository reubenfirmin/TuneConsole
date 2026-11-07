"""Prevent partial-window, identity and averaging errors in the paired study."""
import copy

import numpy as np
import pytest

from experiments.audio_lab_tools.audio import preprocessing_quality as study


def example():
    rows = [{'id': 'song-a', 'audio_sha256': 'abc', 'audio': '/audio/a.mp3',
        'chunk_starts': [15, 25, 35], 'covered_interval': [15, 45],
        'annotation_unit': 'released_interval'}]
    meta = {'rows': rows, 'groups': ['artist-a']}
    arrays = {'embedding': np.zeros((3, 200)), 'offsets': np.array([0, 3]),
        'outer': np.array([0]), 'y': np.array([[.2, .7]])}
    return meta, copy.deepcopy(rows), arrays


def test_complete_window_join_preserves_annotation_and_original_order():
    meta, sources, arrays = example()
    rows = study.bind_rows(meta, sources, arrays, 'manymusic-natural')
    assert rows[0]['starts'] == [15, 25, 35]
    assert rows[0]['complete_annotated_interval']
    assert rows[0]['sentinel_chunk'] == 1
    assert rows[0]['group'] == 'artist-a'


@pytest.mark.parametrize('change', ['hash', 'missing_tail', 'gap', 'offset', 'duplicate_source'])
def test_join_rejects_changed_identity_or_incomplete_interval(change):
    meta, sources, arrays = example()
    if change == 'hash':
        sources[0]['audio_sha256'] = 'different'
    elif change == 'missing_tail':
        meta['rows'][0]['chunk_starts'].pop()
    elif change == 'gap':
        meta['rows'][0]['chunk_starts'][1] += 1
    elif change == 'offset':
        arrays['offsets'][-1] -= 1
    else:
        sources.append(sources[0])
    with pytest.raises(ValueError):
        study.bind_rows(meta, sources, arrays, 'manymusic-natural')


def test_secondary_whole_song_label_remains_a_ten_second_analysis():
    row = {'id': 'song', 'audio_sha256': 'same', 'audio': '/audio.mp3',
        'sample_start': 42.123456789, 'sample_seconds': 10, 'annotation_unit': 'whole_song_weak_label'}
    meta = {'rows': [row], 'groups': ['artist']}
    arrays = {'embedding': np.zeros((1, 200)), 'outer': np.array([0]), 'y': np.zeros((1, 2))}
    case = study.bind_rows(meta, [row], arrays, 'cal500-raw-activity')[0]
    assert not case['complete_annotated_interval']
    assert case['starts'] == [42.123457]
    assert case['annotation_unit'] == 'whole_song_weak_label'


def test_individual_model_receives_each_excerpt_before_aggregation(monkeypatch):
    def shared(model, inputs):
        return inputs[:, :2]
    def cruise(model, chunks):
        # Nonlinear score exposes an incorrect mean-embedding shortcut.
        return {'score': float(np.square(np.asarray(chunks)[:, 0]).mean())}
    monkeypatch.setattr(study, 'predict', shared)
    monkeypatch.setattr(study.audio_vibe_cruise, 'score', cruise)
    values = np.zeros((3, 200))
    values[:, 0] = [0, 2, 4]
    group, individual = study.predictions({}, {}, values, np.array([0, 2, 3]))
    np.testing.assert_array_equal(group[:, 0], [1, 4])
    np.testing.assert_array_equal(individual, [2, 16])


def test_receipt_requires_all_chunks_and_the_declared_production_sentinel(tmp_path):
    case = {'cohort': 'cohort', 'index': 0, 'starts': [15], 'sentinel_chunk': 0}
    target = study.case_path(tmp_path, case)
    target.mkdir(parents=True)
    study.save(tmp_path / 'plan.json', {'fixture': True})
    chunk = {'start': 15, 'browser': {'embedding': [0] * 200},
             'ffmpeg': {'embedding': [0] * 200}}
    result = {'case': case, 'chunks': [chunk]}
    def write():
        study.save(target / 'report.json', result)
        study.save(target / 'receipt.json', {'plan_sha256': study.digest(tmp_path / 'plan.json'),
            'outputs': {'report.json': study.digest(target / 'report.json')}})
    write()
    with pytest.raises(ValueError, match='sentinel'):
        study.checked_case(tmp_path, case)
    chunk['production_sentinel'] = {'maximum_absolute_error': 0.00001}
    write()
    study.checked_case(tmp_path, case)
    result['chunks'] = []
    write()
    with pytest.raises(ValueError, match='Missing or reordered'):
        study.checked_case(tmp_path, case)


def test_receipt_rejects_changed_evidence(tmp_path):
    case = {'cohort': 'cohort', 'index': 0}
    target = study.case_path(tmp_path, case)
    target.mkdir(parents=True)
    study.save(tmp_path / 'plan.json', {'fixture': True})
    study.save(target / 'report.json', {'original': True})
    study.save(target / 'receipt.json', {'plan_sha256': study.digest(tmp_path / 'plan.json'),
        'outputs': {'report.json': study.digest(target / 'report.json')}})
    study.save(target / 'report.json', {'changed': True})
    with pytest.raises(ValueError, match='Pinned input changed'):
        study.checked_case(tmp_path, case)


@pytest.mark.parametrize('position', [float('nan'), float('inf'), -1])
def test_invalid_excerpt_positions_are_rejected(position):
    with pytest.raises(ValueError, match='invalid excerpt'):
        study.offsets_for([position])
