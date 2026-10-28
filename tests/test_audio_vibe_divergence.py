"""Severe feedback contradictions must survive heuristic recomputation and fitting."""
from copy import deepcopy
import json
import sqlite3

import pytest

from experiments.audio_lab_tools import train_vibes as trainer
from tests.test_audio_vibe_model import examples, excerpts
from yt_playlist.rec import audio_vibe_model as model
from yt_playlist.rec import audio_vibe_preview as heuristic


def jazzbois_prediction():
    # Captured scores for The Sneeze: the learned correction rejected Cruise
    # despite the underlying heuristic ranking it first at .94168.
    scores = {'energize': .99653, 'uplift': .70641, 'concentrate': .35464,
              'release': .15977, 'cruise': .10739, 'relax': .0012}
    return {'taxonomy_version': heuristic.TAXONOMY_VERSION, 'version': heuristic.VERSION,
            'vibes': ['energize', 'uplift'],
            'ranking': [{'vibe': key, 'score': value} for key, value in scores.items()],
            'model': {'sha256': 'b' * 64, 'training_songs': 30}}


@pytest.mark.parametrize(('scores', 'human', 'kind'), [
    ({'cruise': .9}, ['cruise'], 'agreement'),
    ({'cruise': .9, 'uplift': .7}, ['cruise'], 'agreement'),
    ({'cruise': .9, 'uplift': .7}, ['cruise', 'relax'], 'agreement'),
    ({'cruise': .9, 'uplift': .7}, ['relax', 'cruise'], 'agreement'),
    ({'cruise': .9, 'uplift': .7}, ['uplift'], 'disjoint'),
    ({'energize': .66, 'cruise': .64}, ['cruise'], 'disjoint'),
    ({'energize': .4, 'cruise': .3}, ['cruise'], 'disjoint'),
    ({'energize': .7, 'cruise': .2}, ['cruise'], 'wide_divergence'),
    ({'energize': .7, 'cruise': .20001}, ['cruise'], 'disjoint'),
    ({'energize': .99, 'cruise': .1, 'uplift': .6}, ['cruise', 'uplift'], 'disjoint'),
])
def test_disagreement_distinguishes_overlap_close_calls_and_large_score_gaps(scores, human, kind):
    # Include low scores for unspecified targets, as real predictions do.
    scores = {**dict.fromkeys(model.LABELS, 0.), **scores}
    result = trainer.disagreement(scores, human)
    assert result['class'] == kind
    assert set(result['missed']) == (set() if kind == 'agreement' else set(human))
    assert set(result['extra']) == set(result['predicted']) - set(human)


def test_jazzbois_snapshot_identifies_severe_learned_reversal():
    prediction = jazzbois_prediction()
    frozen = deepcopy(prediction)
    snapshot = trainer.feedback_snapshot(prediction, ['cruise'])
    assert prediction == frozen
    assert snapshot['class'] == 'wide_divergence'
    assert snapshot['score_gap'] == .88914
    assert snapshot['missed'] == ['cruise']
    assert snapshot['extra'] == ['energize']
    assert snapshot['model'] == prediction['model']


@pytest.mark.parametrize('invalid', ['missing', 'earlier_taxonomy', 'missing_score', 'duplicate_score',
                                    'nonfinite_score', 'unknown_target', 'no_targets', 'invalid_ranking'])
def test_unavailable_snapshots_stay_unknown(invalid):
    prediction = jazzbois_prediction()
    if invalid == 'missing': prediction = None
    if invalid == 'earlier_taxonomy': prediction['taxonomy_version'] = 'earlier'
    if invalid == 'missing_score': prediction['ranking'].pop()
    if invalid == 'duplicate_score': prediction['ranking'].append(prediction['ranking'][0])
    if invalid == 'nonfinite_score': prediction['ranking'][0]['score'] = float('nan')
    if invalid == 'unknown_target': prediction['vibes'] = ['unknown']
    if invalid == 'no_targets': prediction['vibes'] = []
    if invalid == 'invalid_ranking': prediction['ranking'] = None
    assert trainer.feedback_snapshot(prediction, ['cruise']) is None


def test_readonly_loader_preserves_saved_reversal_instead_of_replacing_it_with_heuristic(store, tmp_path):
    samples = excerpts('jazzbois-example')
    for row in samples:
        store.audio_vibes.save_sample(row, 1)
    prediction = jazzbois_prediction()
    store.audio_vibes.save_feedback(samples[0]['run_id'], [row['id'] for row in samples], ['cruise'],
                                    heuristic.TAXONOMY_VERSION, prediction, 2)
    path = tmp_path / 'snapshot.db'
    with sqlite3.connect(path) as destination:
        store.conn.backup(destination)
    before = path.read_bytes()
    records, skipped = trainer.examples_from_db(path)
    assert path.read_bytes() == before
    assert skipped == {}
    assert records[0]['feedback_prediction'] == trainer.feedback_snapshot(prediction, ['cruise'])
    assert records[0]['baseline'] == {row['vibe']: row['score'] for row in
                                    heuristic.predict(samples, use_model=False)['ranking']}
    assert store.audio_vibes.feedback(samples[0]['run_id'])['prediction'] == prediction


def test_report_tracks_frozen_severe_cohort_with_separate_heldout_candidate_results():
    records = examples()
    records[0]['feedback_prediction'] = trainer.feedback_snapshot(jazzbois_prediction(), ['cruise'])
    records[0]['baseline'] = {**dict.fromkeys(model.LABELS, .1), 'cruise': .94168}
    untouched = deepcopy(records)
    learned, report = trainer.train(records)
    assert records == untouched
    review = report['divergence_review']
    assert review['feedback_classes'] == {'wide_divergence': 1}
    assert review['feedback_snapshot_songs'] == 1
    assert review['unavailable_feedback_snapshot_songs'] == 9
    assert review['learned_reversals'] == 1
    case = review['cases'][0]
    assert case['feedback_prediction']['class'] == 'wide_divergence'
    assert case['feedback_prediction']['model']['sha256'] == 'b' * 64
    assert case['heuristic']['class'] == 'agreement'
    cohort = review['feedback_wide_divergence_cohort']
    assert cohort['songs'] == 1
    for inputs in ('combined', 'audio_only'):
        assert set(cohort[inputs]) == set(trainer.CANDIDATES)
        for candidate, metrics in cohort[inputs].items():
            assert metrics['songs'] == 1
            assert sum(metrics['disagreement_classes'].values()) == 1
            results = report['results'] if inputs == 'combined' else report['input_comparison']['audio_only']['results']
            held_out = results[candidate]['predictions'][0]
            assert held_out['training_songs'] == 9
            assert metrics == trainer.metrics([held_out])
    assert cohort['combined']['heuristic']['correct'] == 1
    assert cohort['combined']['heuristic']['disagreement_classes'] == {'agreement': 1}
    assert model.validate(json.loads(json.dumps(learned))) == learned
    # Snapshot diagnostics never become a feature or change the fitting objective.
    without_snapshot = deepcopy(records)
    del without_snapshot[0]['feedback_prediction']
    assert trainer.fit(records) == trainer.fit(without_snapshot)


def test_no_saved_predictions_does_not_invent_a_severe_cohort():
    records = examples()
    results = trainer.evaluate(records)
    review = trainer.divergence_review(records, results, results)
    assert review['cases'] == []
    assert review['unavailable_feedback_snapshot_songs'] == len(records)
    assert review['feedback_wide_divergence_cohort']['songs'] == 0
    for metrics in review['feedback_wide_divergence_cohort']['combined'].values():
        assert metrics['songs'] == 0
        assert metrics['disagreement_classes'] == {}
        assert metrics['maximum_score_gap'] is None
