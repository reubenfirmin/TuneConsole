"""Compare human gold targets with fixed, weighted model silver targets.

The fixed teacher saw some gold songs. Those songs remain training-only in
every inner and outer split; only teacher-unseen human answers are evaluated.
This preserves the supplied silver predictions without teacher-label leakage.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import robust_vibes as robust, train_vibes as trainer
from yt_playlist.rec import audio_vibe_model as model


CANDIDATES = {**robust.CANDIDATES, **{
    f'silver_{loss}_{int(penalty)}_{weight:g}': {
        'regularization': penalty, 'objective': objective, 'silver_weight': weight}
    for loss, objective in (('legacy', 'legacy_binary'), ('nll', 'acceptable_nll'))
    for penalty in (1., 10.) for weight in (.1, .25, .5, 1.)}}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def freeze(dataset_path, predictions_path, teacher_report_path):
    source = json.loads(dataset_path.read_text())
    predicted = json.loads(predictions_path.read_text())
    teacher_report = json.loads(teacher_report_path.read_text())
    teacher_path = Path(predicted['model'])
    content = teacher_path.read_bytes()
    teacher = model.validate(json.loads(content))
    teacher_hash = hashlib.sha256(content).hexdigest()
    evaluation = {'method': teacher_report['evaluation'],
        'selected_candidate': teacher_report['selected_candidate'],
        'results': {name: {key: value for key, value in result.items() if key != 'predictions'}
                    for name, result in teacher_report['results'].items()}}
    if (teacher_hash != predicted['model_sha256'] or teacher['evaluation'] != evaluation
            or teacher['training']['candidate'] != teacher_report['selected_candidate']):
        raise ValueError('Teacher artifact, predictions and training manifest must match')
    teacher_ids = [row['video_id'] for row in teacher_report['manifest']]
    if len(teacher_ids) != len(set(teacher_ids)) or len(teacher_ids) != teacher['training']['songs']:
        raise ValueError('Teacher manifest must identify every training song exactly once')
    pool = source['unlabeled']
    lookup = {row['video_id']: row for row in predicted['predictions']}
    if len(lookup) != len(predicted['predictions']) or set(lookup) != {row['video_id'] for row in pool}:
        raise ValueError('Every silver song requires exactly one supplied prediction')
    silver = []
    for row in pool:
        prediction = lookup[row['video_id']]
        scores = model.scores(teacher, trainer.example_vector(row, teacher['features']), row['baseline'])
        if (scores != prediction['scores'] or trainer.select(scores) != prediction['predicted']
                or prediction['model_sha256'] != teacher_hash):
            raise ValueError('Silver prediction does not replay from the frozen teacher')
        silver.append({**row, 'labels': prediction['predicted'][:], 'target_source': 'model_prediction',
            'teacher_scores': scores, 'teacher_model_sha256': teacher_hash})
    snapshot = {'gold': source['examples'], 'silver': silver,
        'teacher': {'model_sha256': teacher_hash, 'training_video_ids': sorted(teacher_ids),
            'training_songs': len(teacher_ids), 'candidate': teacher['training']['candidate']},
        'provenance': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (dataset_path, predictions_path, teacher_report_path, teacher_path)},
        'silver_generation': 'fixed_existing_teacher_top_one; no iterative relabeling'}
    validate(snapshot)
    return snapshot


def validate(snapshot):
    gold, silver, teacher = snapshot['gold'], snapshot['silver'], snapshot['teacher']
    robust.validate_examples(gold)
    trainer.validate_silver(gold, silver)
    ids = teacher['training_video_ids']
    teacher_hash = teacher['model_sha256']
    if (not silver or not ids or any(not isinstance(video, str) or not video for video in ids)
            or len(ids) != len(set(ids)) or len(ids) != teacher['training_songs']
            or not isinstance(teacher_hash, str) or len(teacher_hash) != 64
            or any(char not in '0123456789abcdef' for char in teacher_hash)
            or set(ids) & {row['video_id'] for row in silver}):
        raise ValueError('Silver songs must be unseen by an identified frozen teacher')
    for row in silver:
        scores = row['teacher_scores']
        if (row['teacher_model_sha256'] != teacher_hash or set(scores) != set(model.LABELS)
                or any(not model.finite(value) or not 0 <= value <= 1 for value in scores.values())
                or trainer.select(scores) != row['labels']):
            raise ValueError('Silver targets and teacher provenance disagree')
    if sum(row['video_id'] not in ids for row in gold) < 4:
        raise ValueError('At least four teacher-unseen gold songs are required for evaluation')


def fit_candidate(gold, silver, parameters):
    if parameters is None:
        return None
    return trainer.fit(gold, **parameters, **({'silver_examples': silver} if parameters.get('silver_weight') else {}))


def select(gold, silver, teacher_ids, candidates, folds):
    eligible = sorted((row for row in gold if row['video_id'] not in teacher_ids),
                      key=lambda row: hashlib.sha256(row['video_id'].encode()).digest())
    folds = min(folds, len(eligible))
    if folds < 2:
        raise ValueError('Selection requires at least two teacher-unseen gold folds')
    predictions = {name: [] for name in candidates}
    for fold in range(folds):
        validation = eligible[fold::folds]
        excluded = {row['video_id'] for row in validation}
        training = [row for row in gold if row['video_id'] not in excluded]
        for name, parameters in candidates.items():
            learned = fit_candidate(training, silver, parameters)
            predictions[name].extend(robust.predict(learned, row, len(training)) for row in validation)
    results = {name: trainer.metrics(rows) for name, rows in predictions.items()}
    return max(results, key=lambda name: robust.selection_key(results[name])), results


def train(snapshot, *, candidates=None, inner_folds=5, progress=None):
    validate(snapshot)
    candidates = CANDIDATES if candidates is None else candidates
    if not candidates or type(inner_folds) is not int or inner_folds < 2:
        raise ValueError('Invalid gold/silver candidate or fold configuration')
    gold, silver = snapshot['gold'], snapshot['silver']
    teacher_ids = set(snapshot['teacher']['training_video_ids'])
    eligible = sorted((row for row in gold if row['video_id'] not in teacher_ids), key=lambda row: row['video_id'])
    families = {'gold_only': [name for name, params in candidates.items() if not (params or {}).get('silver_weight')],
                'gold_silver': [name for name, params in candidates.items() if (params or {}).get('silver_weight')]}
    if not all(families.values()):
        raise ValueError('Both gold-only and gold/silver comparison candidates are required')
    development = {name: [] for name in candidates}
    selections, nested = [], []
    family_rows = {family: [] for family in families}
    with threadpool_limits(limits=1):
        for index, held_out in enumerate(eligible):
            training = [row for row in gold if row['video_id'] != held_out['video_id']]
            best, inner = select(training, silver, teacher_ids, candidates, inner_folds)
            predictions = {}
            for name, parameters in candidates.items():
                learned = fit_candidate(training, silver, parameters)
                predictions[name] = robust.predict(learned, held_out, len(training))
                development[name].append(predictions[name])
            nested.append({**predictions[best], 'selected_candidate': best})
            selections.append({'held_out_video_id': held_out['video_id'], 'selected_candidate': best,
                               'inner_results': inner})
            for family, names in families.items():
                selected = max(names, key=lambda name: robust.selection_key(inner[name]))
                family_rows[family].append({**predictions[selected], 'selected_candidate': selected})
            if progress:
                progress(index + 1, len(eligible), best)
        best, final_selection = select(gold, silver, teacher_ids, candidates, inner_folds)
        selected = {family: max(names, key=lambda name: robust.selection_key(final_selection[name]))
                    for family, names in families.items()}
        exported = {}
        for family, name in selected.items():
            learned = fit_candidate(gold, silver, candidates[name])
            if learned is None:
                learned = trainer.fit(gold, intercept_only=True)
                learned['classifiers'] = {}
            result = trainer.metrics(family_rows[family])
            learned['training'].update({'candidate': name, 'strategy': 'fixed_teacher_gold_silver',
                'gold_dataset_sha256': digest(gold), 'silver_dataset_sha256': digest(silver),
                'teacher_model_sha256': snapshot['teacher']['model_sha256']})
            learned['evaluation'] = {'method': 'nested_leave_one_teacher_unseen_gold_song_out',
                'inner_folds': min(inner_folds, len(eligible) - 1),
                'selection': family + '_candidates', 'results': result}
            exported[family] = model.validate(learned)
    report = {'created_at': datetime.now(timezone.utc).isoformat(),
        'gold_songs': len(gold), 'silver_songs': len(silver), 'evaluation_songs': len(eligible),
        'teacher_known_gold_training_only': sorted(teacher_ids & {row['video_id'] for row in gold}),
        'evaluation': 'nested_leave_one_teacher_unseen_gold_song_out',
        'inner_validation': 'teacher_unseen_gold_only', 'inner_folds': min(inner_folds, len(eligible) - 1),
        'gold_dataset_sha256': digest(gold), 'silver_dataset_sha256': digest(silver),
        'teacher': snapshot['teacher'], 'candidates': candidates, 'selected_candidate': best,
        'selected_family_candidates': selected, 'final_selection_results': final_selection,
        'nested': {**trainer.metrics(nested), 'predictions': nested}, 'outer_selections': selections,
        'family_comparison': {family: {**trainer.metrics(rows), 'predictions': rows,
            'outer_candidate_counts': dict(Counter(row['selected_candidate'] for row in rows))}
            for family, rows in family_rows.items()},
        'development_results': {name: {**trainer.metrics(rows), 'predictions': rows} for name, rows in development.items()},
        'limitations': [
            'Only human answers are evaluated; agreement with silver is not accuracy evidence.',
            'The fixed teacher saw some gold songs; they remain training-only in every split.',
            'This 26-song evaluation cohort differs from the earlier 56-song graph comparison.'
                if len(eligible) == 26 else 'The evaluation cohort excludes teacher training songs.',
            'The gold answers were already available during development; this is not a prospective test.',
            'Silver uses all supplied top-one predictions with a constant weight, without confidence filtering.',
            'Teacher scores are uncalibrated; no probability weighting or iterative pseudo-labeling is used.',
            'No model is activated and no database content is changed.']}
    return exported, report


def predictions(exported, silver):
    rows = []
    for row in silver:
        scores = {family: model.scores(learned, trainer.example_vector(row, learned['features']), row['baseline'])
                  for family, learned in exported.items()}
        rows.append({'video_id': row['video_id'], 'title': row.get('title', ''), 'artist': row.get('artist', ''),
            'silver_target': row['labels'], 'target_source': 'model_prediction',
            'predicted': {family: trainer.select(value) for family, value in scores.items()},
            'scores': scores, 'calibrated': False})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--dataset', type=Path, help='Previous labeled/unlabeled feature snapshot')
    inputs.add_argument('--snapshot', type=Path, help='Replay an already frozen gold/silver snapshot')
    parser.add_argument('--predictions', type=Path, help='Existing teacher predictions for the unlabeled pool')
    parser.add_argument('--teacher-report', type=Path, help='Training manifest belonging to the teacher')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    if bool(args.dataset) != bool(args.predictions and args.teacher_report) or (
            args.snapshot and (args.predictions or args.teacher_report)):
        parser.error('--dataset requires --predictions and --teacher-report; --snapshot replays them frozen')
    paths = [args.dataset or args.snapshot, args.output, args.report, args.report.with_suffix('.dataset.json'),
             args.report.with_suffix('.gold-model.json'), args.report.with_suffix('.silver-model.json')]
    paths.extend(path for path in (args.predictions, args.teacher_report) if path)
    if len({path.resolve() for path in paths}) != len(paths):
        parser.error('Inputs and output files must have distinct paths')
    try:
        snapshot = freeze(args.dataset, args.predictions, args.teacher_report) if args.dataset else json.loads(args.snapshot.read_text())
        validate(snapshot)
        # A teacher file named in the provenance is also a protected input.
        protected = {Path(path).resolve() for path in snapshot.get('provenance', {})}
        if protected & {path.resolve() for path in paths[1:6]}:
            raise ValueError('Output paths must not overwrite frozen teacher inputs')
        dataset_path = args.report.with_suffix('.dataset.json')
        dataset_path.write_text(json.dumps(snapshot, indent=2, allow_nan=False) + '\n')
        exported, report = train(snapshot, progress=lambda done, total, best:
            print(f'Validated {done}/{total} teacher-unseen gold songs: {best}', flush=True))
        report['dataset'] = str(dataset_path)
        report['silver_predictions'] = predictions(exported, snapshot['silver'])
        for family, suffix in (('gold_only', '.gold-model.json'), ('gold_silver', '.silver-model.json')):
            path = args.report.with_suffix(suffix)
            path.write_text(json.dumps(exported[family], indent=2, allow_nan=False) + '\n')
        family = 'gold_silver' if (report['candidates'][report['selected_candidate']] or {}).get('silver_weight') else 'gold_only'
        chosen = {**exported[family], 'evaluation': {**exported[family]['evaluation'],
            'selection': 'all_candidates',
            'results': {key: value for key, value in report['nested'].items() if key != 'predictions'}}}
        args.output.write_text(json.dumps(chosen, indent=2, allow_nan=False) + '\n')
        args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps({'gold_songs': report['gold_songs'], 'silver_songs': report['silver_songs'],
        'evaluation_songs': report['evaluation_songs'], 'selected_candidate': report['selected_candidate'],
        'families': {family: {key: value[key] for key in ('songs', 'correct', 'accuracy')}
                     for family, value in report['family_comparison'].items()},
        'selected_family_candidates': report['selected_family_candidates'],
        'model': str(args.output), 'report': str(args.report)}, indent=2))


if __name__ == '__main__':
    main()
