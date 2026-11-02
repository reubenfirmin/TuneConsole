"""Independent checks against acquired audio and sealed holdout evidence."""
from datetime import datetime
from pathlib import Path

import numpy as np

from experiments.audio_lab_tools import release_extension as m
from experiments.audio_lab_tools.external_experiment import digest
from experiments.audio_lab_tools.focus_experiment import arrays, load
from experiments.audio_lab_tools.quality_round import verify
from experiments.audio_lab_tools.relax_experiment import global_groups


def test_artist_partition_and_global_components_are_disjoint():
    selection = load(m.DEFAULT / 'inputs/selection.json')
    rows = selection['rows']
    train = [r for r in rows if r['partition'] == 'train']
    evaluation = [r for r in rows if r['partition'] == 'evaluation']
    assert not {r['source_artist_id'] for r in train} & {r['source_artist_id'] for r in evaluation}
    assert not {r['component'] for r in train} & {r['component'] for r in evaluation}
    assert len(rows) == 41
    assert len({r['source_artist_id'] for r in rows}) == 34
    full = list(rows)
    for source in (m.JAM, m.SOUND):
        full.extend(load(m.DEFAULT / f'inputs/{source}.json')['rows'])
    for path in sorted((m.DEFAULT / 'inputs/identities').glob('*.json')):
        full.extend(load(path).get('rows', []))
    full.extend(load(m.DEFAULT / 'inputs/local-identities.json')['rows'])
    np.testing.assert_array_equal(global_groups(full)[:len(rows)], [r['component'] for r in rows])


def test_verified_acquisition_and_exact_excerpt_coverage():
    acquired = load(m.DEFAULT / 'acquisition.json')
    assert acquired['network_bytes'] <= m.LIMIT
    tar_hashes = m.checksums(m.DEFAULT / 'inputs/tar-checksums.txt')
    for archive in acquired['archives']:
        assert archive['sha256'] == tar_hashes[archive['name']]
    for row in acquired['rows']:
        assert digest(row['audio']) == row['audio_sha256']
    extraction = load(m.DEFAULT / 'extraction.json')
    for row in extraction['rows']:
        assert row['sample_start'] == (row['recording_duration'] - 10) / 2
        assert row['sample_start'] >= 0
        assert row['sample_start'] + 10 <= row['recording_duration']
    assert len(extraction['rows']) + len(extraction['rejected']) == len(acquired['rows'])
    assert not list(m.DEFAULT.glob('archives-*'))
    assert not list(m.DEFAULT.glob('pcm-*'))


def test_all_fits_exclude_holdout_and_destination_components():
    rows = load(m.DEFAULT / 'extraction.json')['rows']
    held = {r['component'] for r in rows if r['partition'] == 'evaluation'}
    training = load(m.DEFAULT / 'prediction-trace.json')
    assert training['evaluation_targets_opened'] is False
    assert not set(training['training_target_ids_opened']) & {r['id'] for r in rows if r['partition'] == 'evaluation'}
    for domain, trace in training['training'].items():
        fit = set(trace['original_groups']) | set(trace['extension_groups'])
        assert not fit & held
        assert not fit & set(trace['excluded_components'])
        assert trace['alpha'] == 1000
        if domain == 'soundtracks':
            sound = load(m.DEFAULT / 'inputs/selection.json')['components'][m.SOUND]
            assert not fit & set(sound)


def test_predictions_precede_outcomes_and_all_receipts_verify():
    verify(m.DEFAULT, required_stages={'acquisition', 'extraction', 'predictions', 'evaluation'},
           optional_stages={'postrun-source'})
    report = load(m.DEFAULT / 'report.json')
    order = report['outcome_order']
    assert datetime.fromisoformat(order['predictions_sealed_at']) < datetime.fromisoformat(order['heldout_outcomes_opened_at'])
    rows = load(m.DEFAULT / 'extraction.json')['rows']
    np.testing.assert_array_equal(arrays(m.DEFAULT / 'predictions.npz')['new_ids'], [r['id'] for r in rows])
