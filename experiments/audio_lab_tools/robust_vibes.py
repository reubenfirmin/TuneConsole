"""Compare alternative-label losses using nested, song-grouped validation.

uv run python -m experiments.audio_lab_tools.robust_vibes --db /path/state.db \
    --output /tmp/robust-vibes.json --report /tmp/robust-evaluation.json
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import train_vibes as trainer
from yt_playlist.core import audio_embedding, audio_vibe_metadata
from yt_playlist.rec import audio_vibe_model as model


# Keep the search explicit and bounded. All candidates use the same inputs and
# runtime format; neither a held-out answer nor a feedback snapshot is a feature.
CANDIDATES = {
    'heuristic': None,
    'legacy_ridge_1': {'regularization': 1., 'objective': 'legacy_binary'},
    'legacy_hard_10': {'regularization': 10., 'hard_example_weight': 2., 'objective': 'legacy_binary'},
    'nll_prior_1': {'regularization': 1.},
    'nll_prior_10': {'regularization': 10.},
    'nll_direct_1': {'regularization': 1., 'baseline_weight': 0.},
    'nll_direct_10': {'regularization': 10., 'baseline_weight': 0.},
    'libra_prior_1': {'regularization': 1., 'objective': 'acceptable_libra'},
    'libra_prior_10': {'regularization': 10., 'objective': 'acceptable_libra'},
    'libra_direct_1': {'regularization': 1., 'objective': 'acceptable_libra', 'baseline_weight': 0.},
    'libra_direct_10': {'regularization': 10., 'objective': 'acceptable_libra', 'baseline_weight': 0.},
}
EMBEDDING_CANDIDATES = {**CANDIDATES, **{
    f'{features}_{int(penalty)}': {'regularization': penalty, 'baseline_weight': 0., 'feature_set': features}
    for features in ('embeddings_only', 'embeddings_combined') for penalty in (1., 10.)}}


def validate_examples(examples):
    if len(examples) < 4 or len({row['video_id'] for row in examples}) != len(examples):
        raise ValueError('Robust evaluation requires at least four distinct tracks, once each')
    if len({row['source_signature'] for row in examples}) != 1:
        raise ValueError('Cannot mix upstream measurement/model versions')
    for row in examples:
        if (not row['labels'] or not set(row['labels']) <= set(model.LABELS)
                or len(row['labels']) != len(set(row['labels']))
                or len(row['values']) != len(model.FEATURES)
                or any(value is not None and not model.finite(value) for value in row['values'])
                or (row.get('subgenre') is not None and audio_vibe_metadata.subgenre(
                    {'genre': row['subgenre']}) != row['subgenre'])
                or set(row['baseline']) != set(model.LABELS)
                or any(not model.finite(value) or not 0 <= value <= 1 for value in row['baseline'].values())):
            raise ValueError('Invalid robust training example: ' + row['video_id'])


def fit_candidate(training, name, candidates, *, unlabeled_examples=()):
    parameters = candidates[name]
    if parameters is None:
        return None
    parameters = dict(parameters)
    if parameters.pop('use_unlabeled', False):
        parameters['unlabeled_examples'] = unlabeled_examples
    return trainer.fit(training, **parameters)


def predict(learned, held_out, training_songs):
    scores = dict(held_out['baseline'])
    if learned is not None:
        scores = model.scores(learned, trainer.example_vector(held_out, learned['features']), scores)
    return {'video_id': held_out['video_id'], 'title': held_out.get('title', ''),
            'human': held_out['labels'], 'predicted': trainer.select(scores),
            'disagreement': trainer.disagreement(scores, held_out['labels']),
            'training_songs': training_songs,
            'trained_labels': sorted(learned['classifiers']) if learned is not None else []}


def selection_key(result):
    # Sigmoid and softmax score gaps have different scales. Wide-error counts
    # remain diagnostics; using them to break accuracy ties would favor a link.
    return result['accuracy']


def select_candidate(examples, candidates, folds=5, *, unlabeled_examples=()):
    """Select inside the training set only, with deterministic whole-song folds."""
    folds = min(folds, len(examples))
    if folds < 2:
        raise ValueError('Selection requires at least two folds')
    ordered = sorted(examples, key=lambda row: hashlib.sha256(row['video_id'].encode()).digest())
    predictions = {name: [] for name in candidates}
    for fold in range(folds):
        training = [row for i, row in enumerate(ordered) if i % folds != fold]
        validation = [row for i, row in enumerate(ordered) if i % folds == fold]
        for name in candidates:
            learned = fit_candidate(training, name, candidates, unlabeled_examples=unlabeled_examples)
            predictions[name].extend(predict(learned, row, len(training)) for row in validation)
    results = {name: trainer.metrics(rows) for name, rows in predictions.items()}
    return max(results, key=lambda name: selection_key(results[name])), results


def train(examples, *, candidates=None, inner_folds=None, progress=None, unlabeled_examples=()):
    validate_examples(examples)
    trainer.validate_unlabeled(examples, unlabeled_examples)
    candidates = CANDIDATES if candidates is None else candidates
    if inner_folds is None:
        inner_folds = min(5, len(examples) - 1)
    if not candidates or not 2 <= inner_folds <= len(examples) - 1:
        raise ValueError('Invalid robust candidate/fold configuration')
    if any((parameters or {}).get('feature_set', '').startswith('embeddings_') for parameters in candidates.values()):
        signatures = {row.get('embedding_signature') for row in examples}
        if (len(signatures) != 1 or not isinstance(next(iter(signatures)), str)
                or len(next(iter(signatures))) != 64
                or any(c not in '0123456789abcdef' for c in next(iter(signatures)))):
            raise ValueError('Embedding comparison requires one compatible frozen embedding version')
        for row in examples:
            trainer.example_vector(row, audio_embedding.FEATURES)
    # Outer targets never reach inner selection, normalization, class support,
    # subgenre vocabulary, difficulty weighting, or coefficient fitting.
    outer, selections = [], []
    development = {name: [] for name in candidates}
    pool = {'unlabeled_examples': unlabeled_examples} if unlabeled_examples else {}
    with threadpool_limits(limits=1):
        for index, held_out in enumerate(sorted(examples, key=lambda row: row['video_id'])):
            training = [row for row in examples if row['video_id'] != held_out['video_id']]
            best, inner = select_candidate(training, candidates, inner_folds, **pool)
            predictions = {}
            for name in candidates:
                learned = fit_candidate(training, name, candidates, **pool)
                predictions[name] = predict(learned, held_out, len(training))
                development[name].append(predictions[name])
            outer.append({**predictions[best], 'selected_candidate': best})
            selections.append({'held_out_video_id': held_out['video_id'], 'selected_candidate': best,
                               'inner_results': inner})
            if progress:
                progress(index + 1, len(examples), best)
        best, selection = select_candidate(examples, candidates, inner_folds, **pool)
        learned = fit_candidate(examples, best, candidates, **pool)
        if learned is None:
            learned = trainer.fit(examples, intercept_only=True)
            learned['classifiers'] = {}
    encoded = json.dumps(examples, sort_keys=True, allow_nan=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    nested = {**trainer.metrics(outer), 'predictions': outer}
    learned['training'].update({'candidate': best, 'strategy': 'nested_robust_comparison',
        'dataset_sha256': digest, 'data_join': 'track_feedback_and_latest_samples_per_point',
        'latest_feedback_at': max(row['updated_at'] for row in examples)})
    pool_digest = hashlib.sha256(json.dumps(unlabeled_examples, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if unlabeled_examples:
        learned['training']['unlabeled_dataset_sha256'] = pool_digest
    learned['evaluation'] = {'method': 'nested_leave_one_song_out', 'inner_folds': inner_folds,
                             'results': {k: v for k, v in nested.items() if k != 'predictions'}}
    model.validate(learned)
    training_predictions = [predict(learned, row, len(examples)) for row in examples]
    wide = {row['video_id'] for row in examples
            if (row.get('feedback_prediction') or {}).get('class') == 'wide_divergence'}
    correction = [row for row in training_predictions if row['video_id'] in wide]
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'dataset_sha256': digest,
              'songs': len(examples), 'selected_candidate': best,
              'candidates': candidates, 'evaluation': 'nested_leave_one_song_out',
              'inner_folds': inner_folds, 'nested': nested,
              'target_interpretation': 'any_selected_alternative_is_acceptable',
              'primary_metric': 'single_prediction_accuracy',
              'inputs': {'unlabeled_songs': len(unlabeled_examples), 'unlabeled_dataset_sha256': pool_digest,
                         'embedding_capable_songs': sum(row.get('embedding_values') is not None for row in examples),
                         'embedding_dimensions': audio_embedding.DIMENSIONS,
                         'same_tracks_and_excerpts_for_all_candidates': True},
              'outer_candidate_counts': dict(Counter(row['selected_candidate'] for row in outer)),
              'outer_selections': selections, 'final_selection_results': selection,
              'training_fit': {**trainer.metrics(training_predictions), 'predictions': training_predictions},
              'known_wide_divergence': {
                  'songs': len(wide),
                  'corrected_training_fit': {**trainer.metrics(correction), 'predictions': correction},
                  'held_out': trainer.metrics([row for row in outer if row['video_id'] in wide])},
              'development_results': {name: {**trainer.metrics(rows), 'predictions': rows}
                                      for name, rows in development.items()},
              'limitations': [
                  'Nested scores evaluate the selection procedure; individual candidate scores are development diagnostics.',
                  'All tracks were already available during method development; future-track feedback is still required.',
                  'Legacy independent-binary losses are retained only as comparison candidates.',
                  'Legacy difficulty weights use only each training track answer and its unchanged heuristic.',
                  'Score gaps use uncalibrated sigmoid or softmax scales and are not comparable confidence estimates.',
                  'A single prediction matching any acceptable alternative receives full credit.',
                  'Partial captures are included; source playback and feedback playback need not match.',
                  'No model is activated and no database content is changed.']}
    return learned, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--db', type=Path)
    inputs.add_argument('--dataset', type=Path, help='Replay a frozen feature dataset from an earlier comparison')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--inner-folds', type=int,
                        help='Inner song folds; defaults to five, capped by the outer training set size')
    parser.add_argument('--embeddings', action='store_true',
                        help='Compare attributes, frozen embeddings and both on embedding-capable excerpts only')
    args = parser.parse_args(argv)
    dataset_path = args.report.with_suffix('.dataset.json')
    source = args.db or args.dataset
    paths = (source, args.output, args.report, dataset_path)
    if len({path.resolve() for path in paths}) != len(paths):
        parser.error('Input, model, report and dataset output must be different files')
    try:
        if args.db:
            examples, skipped = trainer.examples_from_db(args.db, require_embeddings=args.embeddings)
        else:
            snapshot = json.loads(args.dataset.read_text())
            examples, skipped = snapshot['examples'], snapshot.get('skipped', {})
        validate_examples(examples)
        dataset_path.write_text(json.dumps({'examples': examples, 'skipped': skipped},
                                          indent=2, allow_nan=False) + '\n')
        learned, report = train(examples, inner_folds=args.inner_folds,
            candidates=EMBEDDING_CANDIDATES if args.embeddings else None,
            progress=lambda done, total, best: print(f'Validated {done}/{total}: {best}', flush=True))
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        parser.error(str(error))
    report.update({'skipped': skipped, 'dataset': str(dataset_path)})
    args.output.write_text(json.dumps(learned, indent=2, allow_nan=False) + '\n')
    args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'selected_candidate': report['selected_candidate'],
                      'evaluation': report['evaluation'], 'scores': learned['evaluation']['results'],
                      'model': str(args.output), 'report': str(args.report), 'dataset': str(dataset_path)}, indent=2))


if __name__ == '__main__':
    main()
