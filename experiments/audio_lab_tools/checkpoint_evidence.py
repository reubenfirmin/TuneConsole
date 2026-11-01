"""Collect checksummed #530 evidence without fitting models or selecting winners."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

from .external_experiment import OUT, ROOT, digest, probe, save, stamp

RUN = OUT / 'checkpoint-1'
CORE = 'soundtracks-all+emotify-artist+cal500-raw-activity+pmemo+vgmidi-original+jamendo-human-4shards+deam'


def concise(report):
    """Keep predeclared procedures and expert ablations, not post-hoc winners."""
    result = {key: value for key, value in report.items()
              if key not in ('choices_by_fold', 'teacher_unseen_subset')}
    if 'combination_search' in result:
        result['combination_search'] = {key: value for key, value in result['combination_search'].items()
                                        if key != 'folds'}
    if 'methods' in result:
        result['methods'] = {key: value for key, value in result['methods'].items()
            if key.startswith('expert:') or ':' not in key}
    return result


def verify_outputs(entries, producers):
    changed, superseded = [], []
    for name, outputs in entries:
        for filename, expected in outputs.items():
            path = OUT / filename
            actual = digest(path) if path.is_file() else None
            if actual == expected:
                continue
            producer, replacement = producers[filename]
            item = {'stage': name, 'file': filename, 'expected_sha256': expected,
                    'actual_sha256': actual, 'latest_producer': producer}
            (superseded if actual == replacement else changed).append(item)
    return {'changed_outputs': changed, 'superseded_outputs': superseded}


def followups(read):
    result = {}
    for name, filename in (
            ('reserved', 'reserved-cohort/report.json'),
            ('reserved_posthoc_controls', 'reserved-cohort/posthoc-controls.json'),
            ('excluded_audio', 'excluded-audio.report.json'),
            ('propagation', 'propagation.report.json'),
            ('soundtracks_local_ablation', 'soundtracks-all.local-ablation.report.json'),
            ('pmemo_local_ablation', 'pmemo.local-ablation.report.json'),
            ('previous_library_workflow', 'listening-workflow-index.json'),
            ('historical_paired', 'historical-paired.report.json')):
        if (RUN / filename).is_file():
            result[name] = read('checkpoint-1/' + filename)
    for name, pattern in (('capture', 'browser-capture/*/report.json'),
                          ('fresh', 'library-replay/fresh-*/report.json'),
                          ('startup', 'library-replay/startup-*/report.json'),
                          ('paired_library', 'library-replay/paired-*/report.json'),
                          ('paired_verification', 'library-replay/paired-slices-*/verification.json'),
                          ('paired_constraint_followups', 'library-replay/paired-slices-*/constraint-followup-*/followup.json')):
        attempts = []
        for path in sorted(RUN.glob(pattern)):
            report = read(str(path.relative_to(OUT)))
            if name == 'capture' and path.parent.name.startswith('preprocessing-'):
                continue
            if name == 'fresh':
                report = {**report, 'phases': {phase: {key: value for key, value in stage.items()
                          if key != 'profiles'} for phase, stage in report['phases'].items()}}
            attempts.append({'path': str(path.relative_to(OUT)), 'report': report})
        result[name] = attempts
    result['preprocessing'] = [{'path': str(path.relative_to(OUT)),
        'report': read(str(path.relative_to(OUT)))}
        for path in sorted(RUN.glob('browser-capture/preprocessing-*/report.json'))]
    return result


def excluded_audio():
    """Inspect the two exclusions without replacing any training artifacts."""
    result = {'created_at': stamp(), 'script_sha256': digest(__file__), 'cases': [],
        'policy': 'Inspect retained failed audio and exact original decode commands. No padding, feature extraction, refitting or replacement of completed artifacts.'}
    for name in ('deam', 'merge-quadrants'):
        features = json.loads((OUT / (name + '.features.json')).read_text())
        index = json.loads((OUT / (name + '.index.json')).read_text())
        by_id = {row['id']: row for row in index['rows']}
        for error in features['errors']:
            row = by_id[error['id']]
            if digest(row['audio']) != row['audio_sha256']:
                raise ValueError('Excluded audio differs from the frozen corpus')
            duration, channels = probe(row['audio'])
            start = max(0, (duration - 10) * .5)
            decoded = {}
            for rate, count in ((16000, 1), (44100, channels)):
                command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                    '-ss', f'{start:.6f}', '-i', row['audio'], '-t', '10', '-ar', str(rate),
                    '-ac', str(count), '-f', 'f32le', '-']
                raw = subprocess.check_output(command, timeout=45)
                exact_command = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                    '-ss', f'{start:.6f}', '-i', row['audio'],
                    '-af', f'aresample={rate},atrim=end_sample={rate * 10}',
                    '-ac', str(count), '-f', 'f32le', '-']
                exact = subprocess.check_output(exact_command, timeout=45)
                decoded[str(rate)] = {'channels': count, 'frames': len(raw) // (4 * count),
                    'expected_frames': rate * 10, 'delta_frames': len(raw) // (4 * count) - rate * 10,
                    'exact_sample_trim_without_padding_frames': len(exact) // (4 * count)}
            result['cases'].append({**error, 'dataset': name, 'audio_sha256': row['audio_sha256'],
                'container_duration_s': duration, 'original_center_start_s': start, 'decoded': decoded})
    save(RUN / 'excluded-audio.report.json', result)
    print(json.dumps(result), flush=True)


def reserved_controls():
    """Add descriptive imbalance controls without changing the frozen evaluation."""
    import numpy as np
    from .external_binary_calibration import metrics
    from .external_transfer import SNAPSHOT, VIBES
    plan_path = RUN / 'reserved-cohort/plan.json'
    predictions_path = plan_path.parent / 'predictions.json'
    human_path = RUN / 'inputs/human-labels.json'
    plan = json.loads(plan_path.read_text())
    if digest(SNAPSHOT) != plan['historical_snapshot_sha256'] or digest(human_path) != plan['snapshot_sha256']:
        raise ValueError('Frozen evaluation inputs changed')
    predictions = json.loads(predictions_path.read_text())
    human = json.loads(human_path.read_text())
    historical = [row for row in json.loads(SNAPSHOT.read_text())['gold']
                  if len(row.get('embedding_values', [])) == 200]
    prior_y = np.asarray([[vibe in row['labels'] for vibe in VIBES] for row in historical])
    if {row['video_id'] for row in historical} & set(plan['reserved_ids']):
        raise ValueError('Historical control uses reserved examples')
    observed = np.asarray(predictions['observed'])
    controls = {'historical_prevalence': np.tile(prior_y.mean(0), (len(observed), 1)),
                'uniform': np.full(observed.shape, 1 / len(VIBES))}
    prior_silver = set(human['new_labels_previously_in_silver'])
    result = {'created_at': stamp(), 'script_sha256': digest(__file__),
        'plan_sha256': digest(plan_path), 'prediction_file_sha256': digest(predictions_path),
        'historical_rows': len(historical), 'historical_positives': dict(zip(VIBES, prior_y.sum(0).tolist())),
        'posthoc_descriptive_controls': True, 'model_selected': False,
        'policy': 'Controls added after observing reserved results to expose class imbalance. Prior frequencies use only the original compatible human training set. No changes to the frozen models, predictions, evaluation plan or first report.',
        'limitations': ['This diagnostic was not predeclared and does not provide a new selection set.',
            'Top-choice metrics break ties in canonical vibe order; the separate tie-averaged fraction avoids that arbitrary choice.',
            'Multi-label prevalence is a descriptive control, not calibrated exclusive-class ground truth.'],
        'cohorts': {}}
    for name, retained in (
            ('all_reserved', np.ones(len(observed), dtype=bool)),
            ('no_known_prior_silver', np.asarray([key not in prior_silver for key in predictions['ids']]))):
        y = observed[retained]
        methods = {}
        for method, values in {**controls, 'checkpoint': np.asarray(predictions['predictions']['checkpoint:prediction'])}.items():
            p = values[retained]
            ties = p == p.max(1, keepdims=True)
            methods[method] = {**metrics(y, p),
                'tie_averaged_top_choice_acceptable': float(((y * ties).sum(1) / ties.sum(1)).mean())}
        result['cohorts'][name] = {'rows': len(y), 'positives': dict(zip(VIBES, y.sum(0).tolist())),
            'vibes_with_positive_examples': int((y.sum(0) > 0).sum()), 'methods': methods}
    save(plan_path.parent / 'posthoc-controls.json', result)
    print(json.dumps(result), flush=True)


def summary(result):
    selected = ('checkpoint:prediction', 'core_consensus:selected_candidate',
        'core_selected_learners:selected_candidate', 'core_rankings:selected_candidate',
        'consensus_both-aligned:selected_candidate', 'consensus_perceived:selected_candidate',
        'consensus_windows:selected_candidate', 'consensus_expanded:selected_candidate',
        'expanded_rankings:selected_candidate', 'expanded_semantics:equal_all_activity_support',
        'projection:prediction', 'projection_rankings:prediction',
        'consistency_ordinary_ridge:prediction', 'consistency_unlabeled_consistency:prediction',
        'local_pmemo_ablations:linear_no_local', 'local_pmemo_ablations:stored_teacher_selected',
        'core_semantics:equal_all_activity_support', 'captured_heuristic',
        *(f'{variant}_{kind}_{family}:prediction'
          for variant in ('supervised_shared', 'unlabeled_shared')
          for family in ('shared_native_qualities', 'embedding_shared_qualities')
          for kind in ('gold', 'ranking')))
    reserved = result.get('followups', {}).get('reserved', {})
    captures = result.get('followups', {}).get('capture', [])
    return {'checkpoint': {**{key: result['checkpoint'][key] for key in
                ('state', 'changed_outputs')},
                'explained_supersessions': len(result['checkpoint']['superseded_outputs'])},
        'completion': {key: result.get('completion', {}).get(key) for key in
                ('state', 'changed_outputs', 'superseded_outputs')},
        'missing': result['missing'],
        'capture_cases': [{
            'id': row['id'], 'wall_s': row['browser']['wall_seconds'],
            'cpu_s': row['browser']['sampled_browser_cpu_seconds'],
            'peak_pss_mb': row['browser']['peak_browser_pss_bytes'] / 1e6,
            'matched_pcm_delta': row['native_browser_matched_pcm_delta'],
            'independent_default_resampling_embedding_delta': row['resampling_embedding_delta']}
            for row in (captures[-1]['report']['cases'] if captures else [])],
        'reserved': {cohort: {'rows': item['rows'], 'methods': {name: {
            'macro_ap': round(item['methods'][name]['macro_average_precision'], 5),
            'top_1': round(item['methods'][name]['top_choice']['top_1_acceptable'], 5),
            'nll': round(item['methods'][name]['top_choice']['acceptable_log_loss'], 5),
            'nll_reduction_interval': [round(value, 5) for value in item['methods'][name][
                'versus_checkpoint']['acceptable_nll_reduction']['conditional_95_percent_interval']]}
            for name in selected if name in item['methods']}}
            for cohort, item in reserved.get('cohorts', {}).items() if cohort == 'no_known_prior_silver'},
        'followup_reports': {name: [{'path': row['path'], 'complete': row['report'].get('complete'),
             'error': row['report'].get('error'),
             'runtime_unchanged': row['report'].get('runtime_code_unchanged_during_run',
                 row['report'].get('runtime_and_research_code_unchanged')),
             **{key: row['report'][key] for key in ('extended_publication_diagnostic',
                 'publication_limit_s', 'original_publication_bound_passed',
                 'all_fixed_candidates_attempted', 'all_fixed_candidates_passed',
                 'runtime_sql_models_payloads_receipts_verified',
                 'ordered_playlists_match_failed_attempt', 'warm_retunes_retried',
                 'original_failure_retained', 'sql_unchanged') if key in row['report']}}
            for row in rows] for name, rows in result.get('followups', {}).items() if isinstance(rows, list)}}


def historical_summary(report):
    """Read fixed historical procedures without recalculation or reselection."""
    return {'created_at': report['created_at'], 'rows': report['rows'], 'groups': report['groups'],
        'reference': 'core_consensus expert:embedding; identical outer folds and observations',
        'baseline': {key: report['methods']['core_consensus']['expert:embedding']['metrics'][key]
                     for key in ('macro_average_precision', 'top_choice')},
        'selected_procedures': {name: {
            'macro_ap': methods['selected_candidate']['metrics']['macro_average_precision'],
            'top_choice': methods['selected_candidate']['metrics']['top_choice'],
            'versus_core_embedding': methods['selected_candidate']['versus_core_embedding'],
            'ranking_versus_core_embedding': methods['selected_candidate']['ranking_versus_core_embedding']}
            for name, methods in report['methods'].items()},
        'limitations': report['limitations']}


def collect(*, coverage=False):
    inputs, missing = {}, []

    def read(name):
        path = OUT / name
        if not path.exists():
            missing.append(name)
            return None
        raw = path.read_bytes()
        inputs[name] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    result = {'created_at': stamp(), 'script_sha256': digest(__file__),
              'input_sha256': inputs, 'missing': missing}
    state = read('checkpoint-1/status.json')
    completion_path = RUN / 'completion/status.json'
    completion = read('checkpoint-1/completion/status.json') if completion_path.exists() else None
    latest = {row['name']: row for row in state['steps']}
    result['checkpoint'] = {'state': state['state'], 'started_at': state['started_at'],
        'updated_at': state['updated_at'], 'stages': {}, 'changed_outputs': [], 'superseded_outputs': []}
    producers = {}
    for row in state['steps']:
        if row['state'] == 'complete':
            for filename, expected in row.get('outputs', {}).items():
                producers[filename] = (row['name'], expected)
    # Completion stages deliberately replace some canonical main outputs. Their
    # receipts must match the replacement bytes; a newer filename alone is not
    # evidence that an earlier mismatch is explained.
    completion_latest = {row['name']: row for row in completion['steps']} if completion else {}
    for row in completion_latest.values():
        if row['state'] == 'complete':
            for filename, expected in row.get('outputs', {}).items():
                relative = str((ROOT / filename).relative_to(OUT))
                producers[relative] = ('completion:' + row['name'], expected)
    entries = []
    for name, row in latest.items():
        result['checkpoint']['stages'][name] = {key: row[key] for key in
            ('state', 'started_at', 'finished_at', 'fingerprint', 'error') if key in row}
        entries.append((name, row.get('outputs', {})))
    result['checkpoint'].update(verify_outputs(entries, producers))
    if completion:
        entries = [('completion:' + name, {str((ROOT / filename).relative_to(OUT)): expected
                    for filename, expected in row.get('outputs', {}).items()})
                   for name, row in completion_latest.items()]
        result['completion'] = {'state': completion['state'], 'active': completion.get('active'),
            'updated_at': completion['updated_at'], 'stages': {name: {key: row[key] for key in
                ('state', 'started_at', 'finished_at', 'fingerprint', 'error') if key in row}
                for name, row in completion_latest.items()}, **verify_outputs(entries, producers)}
        comparisons = {}
        for name, row in completion_latest.items():
            if row['state'] != 'complete':
                continue
            reports = {}
            for filename in row.get('outputs', {}):
                path = ROOT / filename
                if path.name.endswith('.report.json') or path.name == 'report.json':
                    relative = str(path.relative_to(OUT))
                    reports[relative] = concise(read(relative))
            comparisons[name] = reports
        result['completion']['comparisons'] = comparisons

    result['native'] = {}
    for name in ('soundtracks-all', 'emotify-artist', 'emotify-artist-multiwindow', 'emotify-plus-strict',
                 'cal500-raw-activity', 'pmemo', 'deam', 'emma-preview-matched', 'merp-static',
                 'merge-quadrants', 'merge-tag-va', 'amg1608-matched', 'ntwicm-matched'):
        report = read(name + '.report.json')
        if report:
            result['native'][name] = {key: report[key] for key in
                ('rows', 'unique_groups', 'extraction_errors', 'baseline', 'families')}
    result['temporal'] = {}
    for name in ('pmemo', 'deam'):
        report = read(name + '.temporal-comparison.json')
        if report:
            result['temporal'][name] = {key: report[key] for key in ('rows', 'groups', 'methods', 'limitations')}
    result['learners'] = {}
    for name in ('emotify-artist', 'pmemo', 'deam', 'pmemo-dynamic', 'deam-dynamic'):
        report = read(name + '.learners.report.json')
        if report:
            result['learners'][name] = report['features']

    result['consensus'] = {}
    for suffix in ('', '__native-selected_learner__calibrator-selected_learner'):
        report = read('consensus-checkpoint530__' + CORE + suffix + '.report.json')
        if report:
            result['consensus'][suffix or 'ridge_linear'] = {
                key: report[key] for key in ('gold_rows', 'native_learner', 'calibrator_learner', 'methods')}
            result['consensus'][suffix or 'ridge_linear']['search'] = {
                key: value for key, value in report['combination_search'].items() if key != 'folds'}
    report = read('vibe-ranking-checkpoint530__' + CORE + '.report.json')
    if report:
        result['ranking'] = {key: report[key] for key in ('rows', 'sources', 'nonempty_subsets') if key in report}
        result['ranking']['methods'] = {key: value for key, value in report['methods'].items()
            if key.startswith('expert:') or key in ('selected_candidate', 'selected_consensus', 'equal_all', 'weighted_all')}
    report = read('checkpoint530-semantic.report.json')
    if report:
        result['fixed_semantics'] = {key: report[key] for key in ('rows', 'gold_training_rows', 'methods')}
    report = read('cross-corpus.report.json')
    if report:
        result['cross_corpus'] = {name: {key: value for key, value in case.items() if key in
            ('state', 'training_rows', 'test_rows', 'methods')} for name, case in report['directions'].items()}
    report = read('merge-4q-expansion.report.json')
    if report:
        result['merge_expansion'] = {key: report[key] for key in ('rows', 'groups', 'methods')}
    for name in ('multitask.report.json', 'soundtracks-all.unlabeled-projection.report.json',
                 'export-replay.report.json'):
        report = read(name)
        if report:
            result[name] = report

    report = read('checkpoint-1/new-corpora/transfer-report.json')
    if report:
        result['new_corpora'] = {'state': report.get('state'), 'limitations': report['limitations'],
            'directions': {name: {key: case[key] for key in
                ('state', 'source_rows', 'source_groups', 'target_rows', 'target_groups', 'source_alpha', 'methods')
                if key in case} for name, case in report['directions'].items()}}
    features = read('checkpoint-1/new-corpora/features.json')
    if features:
        result['new_audio_coverage'] = {'state': features['state'], 'planned': features['total'],
            'usable': dict(Counter(row['subset'] for row in features['rows'])),
            'errors': features['errors']}
        if coverage:
            index = read('checkpoint-1/new-corpora/current-plan.json')
            path = Path(index['path'])
            if digest(path) != index['sha256']:
                raise ValueError('Frozen pilot plan changed')
            planned = {row['id']: row for row in json.loads(path.read_text())['rows']}
            details = []
            for error in features['errors']:
                row = planned[error['id']]
                if digest(row['audio']) != row['audio_sha256']:
                    raise ValueError('Excluded audio changed')
                duration, _ = probe(row['audio'])
                details.append({**error, 'real_duration': duration, 'rated_interval': row['covered_interval'],
                                'shortfall_seconds': row['covered_interval'][1] - duration})
            result['new_audio_coverage']['excluded_interval_details'] = details
    result['followups'] = followups(read)
    result['dataset_leads'] = {name: read('../datasets/discovery/' + name + '/metadata-inspection.json')
        for name in ('song-describer', 'emid', 'globalmood', 'playing-musicians')
        if (OUT / '../datasets/discovery' / name / 'metadata-inspection.json').is_file()}
    path = RUN / 'evidence.json'
    save(path, result)
    print(json.dumps({'evidence': str(path), 'changed_checkpoint_outputs': result['checkpoint']['changed_outputs'],
                      'changed_completion_outputs': result.get('completion', {}).get('changed_outputs'),
                      'missing': missing, 'new_audio_coverage': result.get('new_audio_coverage'),
                      'new_transfer_primary': {name: {'rows': case.get('target_rows'), 'groups': case.get('target_groups'),
                          'mae': {method: value['mae'] for method, value in case.get('methods', {}).items()},
                          'ridge_versus_source_mean': case.get('methods', {}).get('ridge_mean_embedding', {}).get('versus_source_mean')}
                          for name, case in result.get('new_corpora', {}).get('directions', {}).items()
                          if name.endswith('__no_known_corpus_overlap')},
                      'native_embedding_mae': {name: report['families']['embedding']['mae']
                          for name, report in result['native'].items()}}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', action='store_true', help='Read container durations for the rejected pilot files')
    parser.add_argument('--summary', action='store_true', help='Summarize the last collected evidence without recalculating it')
    parser.add_argument('--historical-summary', action='store_true', help='Read the saved historical paired report without fitting, bootstrapping or selecting')
    parser.add_argument('--reserved-controls', action='store_true', help='Add explicitly post-hoc, historical-prior and uniform descriptive controls; never change frozen predictions')
    parser.add_argument('--excluded-audio', action='store_true', help='Probe and count decoded samples for the two failed corpus intervals; no model execution')
    parser.add_argument('--show', help='Read a dot-separated field or slash-separated JSON pointer from the last evidence')
    args = parser.parse_args()
    if args.historical_summary:
        print(json.dumps(historical_summary(json.loads((RUN / 'historical-paired.report.json').read_text())), indent=2), flush=True)
    elif args.excluded_audio:
        excluded_audio()
    elif args.reserved_controls:
        reserved_controls()
    elif args.show:
        value = json.loads((RUN / 'evidence.json').read_text())
        keys = args.show[1:].split('/') if args.show.startswith('/') else args.show.split('.')
        for key in keys:
            key = key.replace('~1', '/').replace('~0', '~')
            value = value[int(key)] if isinstance(value, list) else value[key]
        print(json.dumps(value, indent=2), flush=True)
    elif args.summary:
        print(json.dumps(summary(json.loads((RUN / 'evidence.json').read_text())), indent=2), flush=True)
    else:
        collect(coverage=args.coverage)


if __name__ == '__main__':
    main()
