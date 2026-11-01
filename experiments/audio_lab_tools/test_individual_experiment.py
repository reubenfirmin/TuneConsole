"""Independent identity, native-loss, excerpt and retained-result checks."""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from experiments.audio_lab_tools import individual_protocol as protocol
from experiments.audio_lab_tools.focus_experiment import head_predictions


def test_jamendo_artist_id_excludes_all_songs_even_with_missing_names():
    source = [{'id': 'track_0000123', 'group': 'jamendo:artist_0042', 'artist': ''},
              {'id': 'track_0000456', 'group': 'jamendo:artist_42', 'artist': ''},
              {'id': 'track_0000789', 'group': 'jamendo:artist_43', 'artist': ''}]
    keep, audit = protocol.exclude(source, [{'id': 'manymusic:other', 'group': 'jamendo:artist:42'}])
    assert keep.tolist() == [False, False, True]
    assert audit['training_rows'] == 1


def test_recording_window_anchor_closes_collaborators_but_not_prompt_groups():
    # '+' is the documented PMEmo credit separator; '&' can be a band name.
    source = [{'id': 'clip:a', 'recording_id': 'song:a', 'artist': 'Alpha+Beta', 'group': 'prompt:1'},
              {'id': 'clip:b', 'artist': 'Beta+Gamma', 'group': 'prompt:2'},
              {'id': 'clip:c', 'artist': 'Gamma', 'group': 'prompt:3'},
              {'id': 'clip:d', 'artist': 'Other', 'group': 'prompt:1'}]
    keep, _ = protocol.exclude(source, [{'id': 'different-window', 'recording_id': 'song:a'}])
    assert keep.tolist() == [False, False, False, True]
    assert len(set(protocol.components(source, include_declared=True))) == 1


def test_jamendo_recording_alias_does_not_depend_on_identical_audio_encoding():
    source = [{'id': 'track_0000123', 'group': 'jamendo:artist_42', 'audio_sha256': 'original'},
              {'id': 'track_0000456', 'group': 'jamendo:artist_42'}]
    target = [{'id': 'manymusic:123:20:30', 'source_url': 'http://www.jamendo.com/track/123', 'audio_sha256': 'excerpt'}]
    keep, audit = protocol.exclude(source, target)
    assert keep.tolist() == [False, False]
    assert audit['component_additional_ids'] == ['track_0000456']


def test_native_three_column_fractional_constants_and_independent_export():
    x = np.arange(120, dtype=float).reshape(30, 4)
    y = np.tile([.25, 2 / 3, .8], (30, 1))
    heads, trace = protocol.select(x, y, np.repeat(np.arange(3), 10))
    assert trace['selected'] == [0, 0, 0]
    assert np.allclose(head_predictions(heads, x, True), y, atol=1e-14, rtol=0)


def test_partial_chunks_do_not_borrow_from_the_next_recording():
    values = np.array([[1., 0.], [0., 1.], [.5, .5], [.2, .8]])
    offsets = np.array([0, 1, 4])
    assert np.allclose(protocol.mean_chunks(values, offsets, 3), [[1, 0], [7 / 30, 23 / 30]])
    assert np.array_equal(protocol.mean_chunks(values, offsets, 1), [[1, 0], [0, 1]])


def test_independent_focus_real_cohort_metrics_and_grouping():
    directory = Path(__file__).resolve().parents[1] / 'audio_lab/local/individual-20261030/focus-1'
    if not (directory / 'evaluation.receipt.json').exists():
        pytest.skip('Local frozen research artifacts not available in this checkout')
    import hashlib
    receipt = json.loads((directory / 'evaluation.receipt.json').read_text())
    for name, expected in receipt['outputs'].items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected
    report = json.loads((directory / 'evaluation.report.json').read_text())
    with np.load(directory / 'evaluation.npz') as data:
        y, outer = data['observed'], data['outer']
        assert len(y) == 450 and len(set(y)) > 4
        for method in report['metrics']:
            score = data[method]
            result = []
            for fold in range(5):
                observed, predicted = y[outer == fold].tolist(), score[outer == fold].tolist()
                k = min(10, math.ceil(len(observed) / 5))
                discount = [1 / math.log2(position + 2) for position in range(k)]
                ideal = sum(value * weight for value, weight in zip(sorted(observed, reverse=True), discount))
                gain, top = 0., 0.
                for value, prediction in zip(observed, predicted, strict=True):
                    preceding = sum(other > prediction for other in predicted)
                    tied = sum(other == prediction for other in predicted)
                    gain += value * sum(discount[preceding:min(k, preceding + tied)]) / tied
                    top += value * max(0, min(k, preceding + tied) - preceding) / tied / k
                result.append([gain / ideal, top, sum((a - b) ** 2 for a, b in zip(observed, predicted, strict=True)) / len(observed)])
            expected = np.mean(result, axis=0)
            for name, value in zip(('ndcg', 'top_mean', 'mse'), expected, strict=True):
                assert report['metrics'][method][name] == pytest.approx(value, abs=1e-12)


def test_identity_group_crossing_prior_outer_folds_is_rejected():
    with pytest.raises(ValueError, match='crosses folds'):
        protocol.split_inner(np.array([0, 0, 1, 2, 3, 4]), np.array([0, 1, 1, 2, 3, 4]))


def test_paired_uninformative_reference_stays_unevaluable():
    y = np.ones(10) * .5
    result = protocol.paired(y, {'a': y, 'b': y + .1}, np.repeat(np.arange(5), 2),
                             np.arange(10), [('a', 'b')], draws=3)['a__vs__b']
    assert result['ndcg']['difference'] is None
    assert result['ndcg']['conditional_95_percent_interval'] is None
    assert result['top_lift']['usable_draws'] == 0
    assert result['mse']['difference'] == pytest.approx(-.01)
