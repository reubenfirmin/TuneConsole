"""Focused checks for frozen inputs and original-vote ranking diagnostics."""
import csv
import json
import sqlite3

import numpy as np
import pytest

from experiments.audio_lab_tools import external_checkpoint as checkpoint
from experiments.audio_lab_tools import external_ranking as ranking
from experiments.audio_lab_tools import external_transfer
from experiments.audio_lab_tools.external_experiment import digest, save


def test_freeze_preserves_source_database_and_detects_changed_inputs(store, tmp_path, monkeypatch):
    database = tmp_path / 'source.db'
    with sqlite3.connect(database) as destination:
        store.conn.backup(destination)
    before = digest(database)
    out = tmp_path / 'research'
    out.mkdir()
    historical = out / 'historical.json'
    save(historical, {'gold': [], 'silver': []})
    save(out / 'example.features.json', {'rows': [
        {'id': f'example:{i}', 'group': f'artist:{i}', 'artist': '', 'audio_sha256': str(i)}
        for i in range(6)], 'errors': []})
    monkeypatch.setattr(checkpoint, 'OUT', out)
    monkeypatch.setattr(checkpoint, 'RUN', out / 'checkpoint-1')
    monkeypatch.setattr(external_transfer, 'SNAPSHOT', historical)
    frozen = checkpoint.freeze(database)
    assert digest(database) == before
    assert frozen['human_rows_with_embeddings'] == 0
    assert checkpoint.freeze(database) == frozen
    groups_path = out / 'checkpoint-1/inputs/external-groups.json'
    groups = json.loads(groups_path.read_text())
    assert len({r['fold'] for r in groups['rows']}) == 5
    groups_path.write_text('{}')
    with pytest.raises(ValueError, match='Frozen input changed'):
        checkpoint.freeze(database)
    assert digest(database) == before


def test_unicode_recording_identity_keeps_distinct_non_latin_names():
    assert checkpoint.identity('Björk') == checkpoint.identity('BJO\u0308RK')
    assert checkpoint.identity('東京') != checkpoint.identity('京都')
    assert checkpoint.identity('東京')


def test_completed_stage_requires_intact_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint, 'OUT', tmp_path)
    report = tmp_path / 'report.json'
    report.write_text('{}')
    entry = {'state': 'complete', 'fingerprint': 'inputs', 'outputs': {'report.json': digest(report)}}
    assert checkpoint.reusable(entry, 'inputs')
    assert not checkpoint.reusable(entry, 'changed-inputs')
    report.write_text('{"changed": true}')
    assert not checkpoint.reusable(entry, 'inputs')


def test_evidence_only_explains_supersession_with_matching_replacement_bytes(tmp_path, monkeypatch):
    from experiments.audio_lab_tools import checkpoint_evidence as evidence
    monkeypatch.setattr(evidence, 'OUT', tmp_path)
    path = tmp_path / 'report.json'
    path.write_text('{"revision": 1}')
    original = digest(path)
    path.write_text('{"revision": 2}')
    replacement = digest(path)
    entries = [('original', {'report.json': original}), ('replacement', {'report.json': replacement})]
    producers = {'report.json': ('replacement', replacement)}
    observed = evidence.verify_outputs(entries, producers)
    assert observed['changed_outputs'] == []
    assert [row['stage'] for row in observed['superseded_outputs']] == ['original']
    path.write_text('{"revision": 3}')
    observed = evidence.verify_outputs(entries, producers)
    assert [row['stage'] for row in observed['changed_outputs']] == ['original', 'replacement']
    assert observed['superseded_outputs'] == []
    path.unlink()
    assert all(row['actual_sha256'] is None for row in evidence.verify_outputs(entries, producers)['changed_outputs'])


def test_dependent_steps_reject_partial_extraction_even_with_usable_features(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint, 'OUT', tmp_path)
    expected = [{'id': 'deam:' + str(i)} for i in range(10)]
    save(tmp_path / 'deam.index.json', {'rows': expected})
    save(tmp_path / 'deam.features.json', {'rows': expected[:4], 'errors': []})
    for step in ('temporal-deam', 'learners-deam', 'cross-corpus', 'multitask', 'consensus-core'):
        assert 'deam' in checkpoint.input_corpora(step)
    with pytest.raises(ValueError, match='deam; 4/10 usable, 6 unprocessed'):
        checkpoint.require_complete_corpus('deam')
    save(tmp_path / 'deam.features.json', {'rows': expected[:9], 'errors': [expected[9]]})
    checkpoint.require_complete_corpus('deam')
    save(tmp_path / 'deam.features.json', {'rows': [*expected, expected[0]], 'errors': []})
    with pytest.raises(ValueError, match='partial'):
        checkpoint.require_complete_corpus('deam')


def test_overlap_refresh_binds_shared_audio_and_preserves_initial_manifest(tmp_path, monkeypatch):
    from experiments.audio_lab_tools import external_overlap_manifest as overlaps
    out, run = tmp_path / 'research', tmp_path / 'research/checkpoint-1'
    run.mkdir(parents=True)
    original = run / 'inputs/external-groups.json'
    save(original, {'original': True})
    original_hash = digest(original)
    feature = out / 'audio.json'
    save(feature, {'embedding': [1., 2.]})
    for task in ('one', 'two'):
        save(out / (task + '.features.json'), {'rows': [
            {'id': f'{task}:{i}', 'group': f'{task}:artist:{i}', 'artist': '',
             'audio_sha256': str(i), 'feature_file': str(feature)} for i in range(10)]})
    monkeypatch.setattr(overlaps, 'OUT', out)
    monkeypatch.setattr(overlaps, 'RUN', run)
    first = overlaps.freeze()
    observed = json.loads(__import__('pathlib').Path(first['manifest']).read_text())
    left = {row['id'].split(':')[-1]: (row['group'], row['fold'])
            for row in observed['rows'] if row['dataset'] == 'one'}
    right = {row['id'].split(':')[-1]: (row['group'], row['fold'])
             for row in observed['rows'] if row['dataset'] == 'two'}
    assert left == right
    assert len({fold for _, fold in left.values()}) == 5
    save(feature, {'embedding': [3., 4.]})
    assert overlaps.freeze()['manifest'] != first['manifest']
    assert digest(original) == original_hash


def test_raw_activity_ranking_uses_original_votes_and_tie_aware_lift(tmp_path, monkeypatch):
    out, data = tmp_path / 'out', tmp_path / 'data'
    out.mkdir()
    annotations = data / 'cal500/annotations-source'
    annotations.mkdir(parents=True)
    # The published matrix discards the only Yes vote for the first song.
    # Original-vote ranking must recover it without indexing the larger matrix.
    (annotations / 'songNames.txt').write_text('a-one\nb-two\nc-three\n')
    (annotations / 'hardAnnotations.txt').write_text('0,1\n1,0\n0,0\n')
    names = ['Usage-Studying']
    observed = [1 / 3, 2 / 3, 0]
    save(out / 'cal500-raw-activity.features.json', {'rows': [
        {'id': f'cal500:{name}', 'target_names': names, 'targets': [value]}
        for name, value in zip(('a-one', 'b-two', 'c-three'), observed)]})
    with (out / 'cal500-raw-activity.oof.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'group', 'fold', 'target', 'observed', 'attributes', 'embedding'])
        for name, value, score in zip(('a-one', 'b-two', 'c-three'), observed, (.5, .5, .1)):
            writer.writerow([f'cal500:{name}', name, 0, names[0], value, score, score])
    monkeypatch.setattr(ranking, 'OUT', out)
    monkeypatch.setattr(ranking, 'DATA', data)
    result = ranking.evaluate('cal500-raw-activity')
    activity = result['activities']['Usage-Studying']
    assert activity['positives'] == 2
    assert activity['families']['embedding']['average_precision'] == 1
    assert activity['families']['embedding']['top_ten_vote_lift_vs_random'] == pytest.approx(1)
    assert ranking.top_mean(np.array([0., 1., .5]), np.array([1., 1., 0.]), count=1) == .5
