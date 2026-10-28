"""Compare attribute classifiers with unlabeled-song similarity regularization.

uv run python -m experiments.audio_lab_tools.semisupervised_vibes \
    --db /path/state.db --output /tmp/vibe-model.json --report /tmp/vibe-report.json

Unlabeled captures provide graph geometry, never pseudo-labels. All fitted
statistics and graph edges exclude validation songs, including inner folds.
The resulting linear coefficients use the existing JSON inference runtime.
"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from experiments.audio_lab_tools import robust_vibes as robust, train_vibes as trainer
from yt_playlist.core import audio_vibe_metadata
from yt_playlist.rec import audio_vibe_model as model, audio_vibe_preview as heuristic


# The training-only graph is an ablation: any benefit from smoothing the known
# songs must not be attributed to additional unlabeled captures.
CANDIDATES = {**robust.CANDIDATES, **{
    f'graph_{pool}_{loss}_{weight:g}': {
        'regularization': 1., 'objective': objective, 'manifold_weight': weight,
        'use_unlabeled': pool == 'unlabeled'}
    for pool in ('training', 'unlabeled')
    for loss, objective in (('legacy', 'legacy_binary'), ('nll', 'acceptable_nll'))
    for weight in (.1, 1.)}}


def collect(path, *, max_unlabeled=512):
    """Freeze human targets, then read external immutable captures read-only.

    Labels and labeled measurements are pinned by the existing trainer's read
    transaction. External captures are read in a second read-only transaction;
    the saved feature dataset is the reproducible input to the entire run.
    """
    if type(max_unlabeled) is not int or not 1 <= max_unlabeled <= 1536:
        raise ValueError('Unlabeled capture limit must be between 1 and 1536')
    examples, skipped = trainer.examples_from_db(path)
    robust.validate_examples(examples)
    known = {row['video_id'] for row in examples}
    source = examples[0]['source_signature']
    omitted = Counter()
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        videos = {row[0] for row in conn.execute('SELECT DISTINCT video_id FROM sample_attributes')} - known
        # Deterministic bounded work; additional captures enter the next run.
        ordered = sorted(videos, key=lambda video: hashlib.sha256(video.encode()).digest())
        omitted['beyond_unlabeled_limit'] = max(0, len(ordered) - max_unlabeled)
        samples, signatures = trainer.track_samples(conn, ordered[:max_unlabeled], omitted)
        unlabeled = []
        for video in ordered[:max_unlabeled]:
            if video not in samples:
                continue
            if signatures[video] != source:
                omitted['incompatible_unlabeled_version'] += 1
                continue
            measurements = sorted(samples[video].values(), key=lambda pair: (
                pair[0]['position_seconds'], pair[0]['target_seconds'], pair[0]['id']))
            excerpts = [value for value, _ in measurements]
            metadata = audio_vibe_metadata.read(conn, video)
            baseline = heuristic.predict(excerpts, use_model=False, require_complete=False)
            unlabeled.append({'video_id': video, 'title': excerpts[0].get('title', ''),
                'artist': excerpts[0].get('artist', ''), 'sample_ids': [row['id'] for row in excerpts],
                'sample_points': [row['point'] for row in excerpts],
                'sampled_at': [at for _, at in measurements], 'source_signature': source,
                'values': model.vector(excerpts, baseline, metadata),
                'metadata_sha256': hashlib.sha256(json.dumps(metadata, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                'subgenre': audio_vibe_metadata.subgenre(metadata),
                'baseline': {row['vibe']: row['score'] for row in baseline['ranking']}})
    trainer.validate_unlabeled(examples, unlabeled)
    return {'examples': examples, 'unlabeled': unlabeled, 'skipped': skipped,
            'unlabeled_skipped': {key: count for key, count in omitted.items() if count}}


def family_comparison(report):
    families = {'supervised': [key for key in report['candidates'] if not key.startswith('graph_')],
                'training_graph': [key for key in report['candidates'] if key.startswith('graph_training_')],
                'unlabeled_graph': [key for key in report['candidates'] if key.startswith('graph_unlabeled_')]}
    result = {}
    for family, names in families.items():
        if not names:
            continue
        lookup = {name: {row['video_id']: row for row in report['development_results'][name]['predictions']}
                  for name in names}
        predictions = []
        for selection in report['outer_selections']:
            best = max(names, key=lambda name: robust.selection_key(selection['inner_results'][name]))
            predictions.append({**lookup[best][selection['held_out_video_id']], 'selected_candidate': best})
        result[family] = {**trainer.metrics(predictions), 'predictions': predictions,
                         'outer_candidate_counts': dict(Counter(row['selected_candidate'] for row in predictions))}
    return result


def unlabeled_predictions(learned, rows):
    """Inspectable experiment output, kept separate from every training target."""
    predictions = []
    for row in rows:
        scores = model.scores(learned, trainer.example_vector(row, learned['features']), row['baseline'])
        predictions.append({'video_id': row['video_id'], 'title': row.get('title', ''),
            'artist': row.get('artist', ''), 'predicted': trainer.select(scores), 'scores': scores,
            'candidate': learned['training']['candidate'], 'calibrated': False,
            'source': 'offline_experiment_prediction', 'used_as_training_target': False})
    return predictions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--db', type=Path)
    inputs.add_argument('--dataset', type=Path, help='Replay a frozen labeled/unlabeled feature dataset')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--inner-folds', type=int)
    parser.add_argument('--max-unlabeled', type=int, default=512)
    args = parser.parse_args(argv)
    dataset_path = args.report.with_suffix('.dataset.json')
    paths = (args.db or args.dataset, args.output, args.report, dataset_path)
    if len({path.resolve() for path in paths}) != len(paths):
        parser.error('Input, model, report and dataset output must be different files')
    try:
        snapshot = (collect(args.db, max_unlabeled=args.max_unlabeled) if args.db
                    else json.loads(args.dataset.read_text()))
        examples, unlabeled = snapshot['examples'], snapshot['unlabeled']
        robust.validate_examples(examples)
        trainer.validate_unlabeled(examples, unlabeled)
        dataset_path.write_text(json.dumps(snapshot, indent=2, allow_nan=False) + '\n')
        learned, report = robust.train(examples, candidates=CANDIDATES, inner_folds=args.inner_folds,
            unlabeled_examples=unlabeled,
            progress=lambda done, total, best: print(f'Validated {done}/{total}: {best}', flush=True))
        learned['training']['strategy'] = 'nested_manifold_comparison'
        report.update({'created_at': datetime.now(timezone.utc).isoformat(), 'dataset': str(dataset_path),
            'skipped': snapshot.get('skipped', {}), 'unlabeled_skipped': snapshot.get('unlabeled_skipped', {}),
            'family_comparison': family_comparison(report),
            'unlabeled_predictions': unlabeled_predictions(learned, unlabeled),
            'experiment': 'unlabeled_similarity_graph_regularization',
            'unlabeled_targets': 'none; human answers are the only supervised targets'})
        report['limitations'] += [
            'Similarity of measured features is an assumption, not proof of shared vibe.',
            'Training-only graph results isolate smoothing from the effect of extra unlabeled songs.',
            'Graph candidates use attributes and catalog metadata, not CNN embeddings.',
            'Current runtime predictions and developer-feedback snapshots are not training targets.']
        args.output.write_text(json.dumps(learned, indent=2, allow_nan=False) + '\n')
        args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        parser.error(str(error))
    print(json.dumps({'selected_candidate': report['selected_candidate'], 'songs': len(examples),
        'unlabeled_songs': len(unlabeled), 'nested': {key: report['nested'][key] for key in ('correct', 'accuracy')},
        'families': {name: {key: value[key] for key in ('correct', 'accuracy')}
                     for name, value in report['family_comparison'].items()},
        'model': str(args.output), 'report': str(args.report), 'dataset': str(dataset_path)}, indent=2))


if __name__ == '__main__':
    main()
