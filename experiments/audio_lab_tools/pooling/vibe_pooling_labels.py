"""Freeze categorical judgments and evaluate original-only metadata transfer.

Unselected vibes remain unknown. Observed-positive retrieval is descriptive;
it cannot establish precision, negative-label accuracy or score calibration.
New identities stay reserved until a candidate/code/configuration seal exists.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import time

import numpy as np

from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.rec import home_vibe, vibe_joint_model as joint, vibe_prediction as baseline
from yt_playlist.rec import vibe_evaluation
from . import vibe_pooling_evaluation as evaluation

FORMAT = 'categorical-vibe-pooling-1'
LABEL_POLICY = 'latest current-taxonomy judgment per canonical recording; unselected vibes unknown'


def identity():
    names = (*evaluation.CODE, 'experiments/audio_lab_tools/pooling/vibe_pooling_labels.py',
             'src/yt_playlist/core/audio_annotations.py', 'src/yt_playlist/core/catalog_keys.py')
    return {name: evaluation.digest((evaluation.ROOT / name).read_bytes()) for name in names}


def previous_labels(paths):
    result = []
    teacher_path = evaluation.ROOT / 'src/yt_playlist/rec/data/emotify-vibes.json'
    teacher = json.loads(teacher_path.read_text())
    for path in paths:
        value = json.loads(path.read_text())
        rows = value.get('rows', value.get('gold'))
        if not isinstance(rows, list) or any(not isinstance(row.get('video_id'), str) for row in rows):
            raise ValueError('Previous labels require explicit video identities: ' + str(path))
        source_hash = evaluation.digest(path.read_bytes())
        trained = source_hash == teacher['provenance']['snapshot_sha256']
        result.append({'path': str(path), 'sha256': source_hash,
            'teacher_sha256': evaluation.digest(teacher_path.read_bytes()) if trained else None,
            'rows': [{'video': row['video_id'], 'labels': row['labels'],
                      'kind': 'human', 'upstream_training': trained and len(row.get('embedding_values', [])) == 200,
                      'updated_at': row.get('updated_at')} for row in rows]
                    + [{'video': row['video_id'], 'labels': row.get('labels', []),
                        'kind': 'prediction', 'upstream_training': False, 'updated_at': None}
                       for row in value.get('silver', [])]})
    return result


def targets(annotations, histories):
    revisions = annotations['revisions']
    superseded = {row['supersedes_id'] for row in revisions if row['supersedes_id'] is not None}
    latest, gaps = {}, []
    for row in sorted(revisions, key=lambda item: (item['created_at'], item['id'])):
        if row['id'] in superseded or row['kind'] != 'session' or row['taxonomy_version'] != TAXONOMY_VERSION:
            continue
        if row['recording'] is None:
            gaps.append({'annotation': row['id'], 'state': row['state'], 'reason': 'unresolved recording identity'})
        else:
            latest[row['recording']] = row
    inspected = defaultdict(list)
    for history in histories:
        for row in history['rows']:
            recording = annotations['video_recordings'].get(row['video'])
            if recording is not None:
                inspected[recording].append({'source_sha256': history['sha256'], **row})
    result = []
    for recording, row in sorted(latest.items()):
        if row['state'] != 'labeled' or not row['labels']:
            gaps.append({'recording': recording, 'annotation': row['id'], 'state': row['state'],
                         'reason': 'latest judgment supplies no positive targets'})
            continue
        if len(set(row['labels'])) != len(row['labels']) or set(row['labels']) - home_vibe.PRESETS.keys():
            raise ValueError('Invalid current categorical annotation')
        old = inspected.get(recording, [])
        result.append({'recording': recording, 'annotation': row['id'], 'labels': row['labels'],
            'updated_at': row['created_at'], 'partition': 'development' if old else 'reserved',
            'previously_inspected': bool(old), 'previous_labels': old,
            'upstream_training': any(item.get('upstream_training') for item in old),
            'prior_prediction_exposure': any(item.get('kind') == 'prediction' for item in old),
            'label_changed': any(item.get('kind', 'human') == 'human'
                                 and set(item['labels']) != set(row['labels']) for item in old)})
    return result, gaps


def freeze(database, destination, histories):
    if not histories:
        raise ValueError('Reserve eligibility requires the previous inspected label snapshots')
    videos = {row['video'] for history in histories for row in history['rows']}
    snapshot = vibe_evaluation.evaluation_snapshot(database, tuple(home_vibe.PRESETS),
                                                  annotations=True, historical_videos=videos)
    humans, gaps = targets(snapshot['annotations'], histories)
    metadata = {}
    for values in snapshot['rows'].values():
        for row in values:
            value = {key: row[key] for key in ('recording', 'artists', 'genre', 'album')}
            if metadata.setdefault(row['recording'], value) != value:
                raise ValueError('Conditioning metadata differs across a recording\'s channels')
    # Use the same current conditioning as the measurement evaluation whenever
    # present. Labels without an original remain independently scoreable.
    for recording, row in snapshot['annotation_conditioning'].items():
        metadata.setdefault(int(recording), row)
    plan = {**snapshot, 'format': FORMAT, 'code': identity(), 'candidate': joint.configuration(),
            'criteria': evaluation.CRITERIA, 'label_policy': LABEL_POLICY,
            'histories': histories, 'human_targets': humans, 'label_gaps': gaps,
            'metadata': {str(key): value for key, value in sorted(metadata.items())},
            'assignments': evaluation.assignments(list(metadata.values())),
            'profile_mask': evaluation.PROFILE_MASK,
            'target_mask': 'all human recordings excluded from every fit; complete artist components outside unseen folds',
            'limits': ['Previously inspected labels are development evidence.',
                'Reserved means absent from supplied historical identities, not proof of no previous inspection.',
                'Historical upstream/backbone and unrecorded exposure remain unknown.',
                'No categorical judgment is a numeric suitability target or a runtime observation.']}
    evaluation.save_new(destination, plan)
    return inventory(plan, evaluation.digest(destination.read_bytes()))


def inventory(plan, plan_hash=None):
    humans = plan['human_targets']
    return {'plan_sha256': plan_hash, 'label_policy': LABEL_POLICY,
        'annotation_revisions': len(plan['annotations']['revisions']),
        'labeled_recordings': len(humans), 'partition_counts': dict(Counter(row['partition'] for row in humans)),
        'changed_recording_labels': sum(row['label_changed'] for row in humans),
        'upstream_training_recordings': sum(row['upstream_training'] for row in humans),
        'prior_prediction_exposure_recordings': sum(row['prior_prediction_exposure'] for row in humans),
        'unresolved_historical_videos': sorted({row['video'] for history in plan['histories'] for row in history['rows']
            if row['video'] not in plan['annotations']['video_recordings']}),
        'positive_counts': {partition: dict(Counter(vibe for row in humans if row['partition'] == partition
                                                    for vibe in row['labels']))
                            for partition in ('development', 'reserved')},
        'original_counts': {vibe: len(rows) for vibe, rows in plan['rows'].items()},
        'source_counts': {vibe: dict(Counter(row['source'].split('/', 1)[0] for row in rows))
                          for vibe, rows in plan['rows'].items()},
        'gaps': plan['label_gaps'], 'limits': plan['limits']}


def load(path):
    plan = json.loads(path.read_text())
    if (plan.get('format') != FORMAT or plan['code'] != identity()
            or plan['candidate'] != joint.configuration() or plan['criteria'] != evaluation.CRITERIA
            or plan['profile_mask'] != evaluation.PROFILE_MASK or plan['label_policy'] != LABEL_POLICY):
        raise ValueError('Categorical evaluation inputs/code changed; freeze a separate plan')
    if set(plan['metadata']) != set(plan['assignments']) or set(plan['metadata']) != set(plan['profile_inputs']):
        raise ValueError('Every target requires exact metadata, assignment and masked conditioning')
    for value in plan['profile_inputs'].values():
        if (set(value['overrides']) - {'genre'}
                or any(source['scope'] not in {'artist', 'release'} for source in value['sources'])
                or any(features.get('audio') for features in value['content'].values() if features is not None)):
            raise ValueError('Target observations leaked into frozen profile conditioning')
    return plan


def seal(path, output):
    plan = load(path)
    value = {'format': 'categorical-vibe-candidate-seal-1', 'plan_sha256': evaluation.digest(path.read_bytes()),
             'code': identity(), 'candidate': plan['candidate'], 'criteria': plan['criteria'],
             'policy': 'One sealed candidate; report all reserved outcomes without refitting or selection.'}
    evaluation.save_new(output, value)
    return value


def run(path, output, stratum, fold, vibe, partition='development', candidate_seal=None):
    plan = load(path)
    plan_hash = evaluation.digest(path.read_bytes())
    if partition == 'reserved':
        if candidate_seal is None:
            raise ValueError('Reserved judgments require a prospective candidate seal')
        sealed = json.loads(candidate_seal.read_text())
        if (sealed['plan_sha256'] != plan_hash or sealed['code'] != identity()
                or sealed['candidate'] != plan['candidate'] or sealed['criteria'] != plan['criteria']):
            raise ValueError('Reserved candidate differs from its seal')
    hidden = {int(key) for key, value in plan['assignments'].items() if value[stratum] == fold}
    human_ids = {row['recording'] for row in plan['human_targets']}
    training = [row for row in plan['rows'][vibe] if row['recording'] not in hidden | human_ids]
    selected = [row for row in plan['human_targets'] if row['partition'] == partition and row['recording'] in hidden]
    started = time.monotonic()
    try:
        with work_budget(10):
            value = joint.fit(training, vibe)
    except WorkBudgetExceeded as exc:
        report = {'format': FORMAT, 'plan_sha256': plan_hash, 'code': identity(), 'partition': partition,
            'stratum': stratum, 'fold': fold, 'vibe': vibe, 'outcome': 'deferred',
            'training_recordings': len(training), 'fit_seconds': time.monotonic() - started,
            'rows': [], 'gaps': [{'reason': str(exc)}]}
        evaluation.save_new(output, report)
        return {key: value for key, value in report.items() if key != 'code'}
    fit_seconds = time.monotonic() - started
    posterior = joint.Posterior(value) if value else None
    stats = {}
    for row in training:
        artist = json.dumps(row['artists'], separators=(',', ':')) if row['artists'] else None
        for group in baseline.groups(artist, row['genre']):
            stats[group] = baseline.adjust(stats.get(group, {}), {'fits': {vibe: row}}, 1)
    train_artists = {artist for row in training for artist in row['artists']}
    rows, gaps = [], []
    for human in selected:
        recording = human['recording']
        row = plan['metadata'][str(recording)]
        known = bool(train_artists.intersection(row['artists']))
        if not row['artists'] or stratum == 'known' and not known:
            gaps.append({'recording': recording, 'reason': 'no artist' if not row['artists'] else 'artist absent from fitting'})
            continue
        if stratum == 'unseen' and known:
            raise ValueError('Artist leaked across a wholly unseen fold')
        profile = evaluation.masked_profile(plan['profile_inputs'][str(recording)])
        if profile is None:
            gaps.append({'recording': recording, 'reason': 'no masked production profile'})
            continue
        artist = json.dumps(row['artists'], separators=(',', ':'))
        fallback = home_vibe.heuristic_affinity(profile, vibe)
        old = baseline.resolve({vibe: fallback}, None,
            [(key, stats[key]) for key in baseline.groups(artist, row['genre']) if key in stats])['fits'][vibe]['score']
        estimate = posterior.predict(row['artists'], row['genre'], album=row['album'], recording=recording) if posterior else None
        rows.append({'recording': recording, 'cluster': plan['assignments'][str(recording)]['cluster'],
            'labels': human['labels'], 'baseline': old, 'candidate': estimate['score'] if estimate else old,
            'source': 'joint' if estimate else 'incumbent-fallback'})
    report = {'format': FORMAT, 'plan_sha256': plan_hash, 'code': identity(), 'partition': partition,
              'stratum': stratum, 'fold': fold, 'vibe': vibe, 'training_recordings': len(training),
              'fit_seconds': fit_seconds, 'wall_seconds': time.monotonic() - started,
              'outcome': 'complete', 'rows': rows, 'gaps': gaps,
              'diagnostics': value['diagnostics'] if value else None}
    evaluation.save_new(output, report)
    return {key: value for key, value in report.items() if key not in ('rows', 'code', 'diagnostics')}


def categorical_metrics(records):
    """Measure observed-positive coverage, without fabricating negative truth."""
    if not records:
        return None
    result = {'recordings': len(records), 'artist_clusters': len({row['cluster'] for row in records})}
    for policy in ('baseline', 'candidate'):
        hits, recall = defaultdict(list), defaultdict(list)
        for row in records:
            ranked = sorted(home_vibe.PRESETS, key=lambda vibe: (-row[policy][vibe], vibe))
            labels = set(row['labels'])
            for k in (1, 2, 3):
                recovered = len(labels.intersection(ranked[:k]))
                hits[k].append(float(recovered > 0))
                recall[k].append(recovered / len(labels))
        positive_retrieval = {}
        for vibe in home_vibe.PRESETS:
            ordered = sorted(records, key=lambda row: (-row[policy][vibe], row['recording']))
            ranks = [index + 1 for index, row in enumerate(ordered) if vibe in row['labels']]
            positive_retrieval[vibe] = {'observed_positives': len(ranks),
                'mean_positive_rank': float(np.mean(ranks)) if ranks else None,
                'observed_positive_recall_at_10': sum(rank <= 10 for rank in ranks) / len(ranks) if ranks else None}
        result[policy] = {'annotated_choice_coverage': {str(k): float(np.mean(v)) for k, v in hits.items()},
            'annotated_vibe_recall': {str(k): float(np.mean(v)) for k, v in recall.items()},
            'within_vibe_observed_positive_retrieval': positive_retrieval}
    result['limits'] = ['Unselected vibes are unknown, never negative targets.',
        'Choice coverage compares raw suitability ranks; independent specialist scales are not class probabilities.',
        'Positive retrieval uses this fixed judged pool and does not establish library precision.',
        'No categorical label establishes numeric suitability calibration.']
    return result


def summarize(path, results, output):
    plan = load(path)
    plan_hash, tasks, records, gaps = evaluation.digest(path.read_bytes()), {}, defaultdict(dict), []
    for source in results:
        report = json.loads(source.read_text())
        if report['plan_sha256'] != plan_hash or report['code'] != identity():
            raise ValueError('Categorical result belongs to another plan/code')
        key = (report['partition'], report['stratum'], report['fold'], report['vibe'])
        if key in tasks:
            raise ValueError('Duplicate categorical evaluation task')
        tasks[key] = report
        gaps.extend({'task': list(key), **gap} for gap in report['gaps'])
        for row in report['rows']:
            record = records[report['partition'], report['stratum']].setdefault(row['recording'],
                {'recording': row['recording'], 'cluster': row['cluster'], 'labels': row['labels'],
                 'baseline': {}, 'candidate': {}})
            if record['labels'] != row['labels'] or record['cluster'] != row['cluster']:
                raise ValueError('Categorical target identity differs across vibes')
            for policy in ('baseline', 'candidate'):
                record[policy][report['vibe']] = row[policy]
    partitions = sorted({key[0] for key in tasks})
    expected = {(p, s, f, v) for p in partitions for s in ('known', 'unseen') for f in range(5) for v in home_vibe.PRESETS}
    metrics = {}
    for partition in partitions:
        metrics[partition] = {}
        for stratum in ('known', 'unseen'):
            complete = []
            for row in records[partition, stratum].values():
                missing = home_vibe.PRESETS.keys() - row['baseline'].keys()
                if missing:
                    gaps.append({'partition': partition, 'stratum': stratum, 'recording': row['recording'],
                                 'reason': 'incomplete six-vibe prediction', 'missing_vibes': sorted(missing)})
                else:
                    complete.append(row)
            metrics[partition][stratum] = categorical_metrics(complete)
    report = {'format': FORMAT, 'plan_sha256': plan_hash,
              'complete': bool(tasks) and set(tasks) == expected
                    and all(row['outcome'] == 'complete' for row in tasks.values()),
              'inventory': inventory(plan), 'metrics': metrics, 'gaps': gaps,
              'candidate_selected': False, 'statistical_qualification': False}
    evaluation.save_new(output, report)
    return report


def diagnose(path, results, output):
    """Inspect an already consumed development campaign without any new fit.

    Historical code hashes are retained as evidence, not asserted to match this
    checkout. Target truth and recording/fold identities must still match the
    frozen originals. Nothing in this diagnostic can qualify a candidate.
    """
    plan = json.loads(path.read_text())
    grouped, tasks, inputs = defaultdict(list), set(), {}
    for vibe, rows in plan['rows'].items():
        inputs[vibe] = {row['recording']: row for row in rows}
    hashes = {}
    for source in results:
        raw = source.read_bytes()
        value = json.loads(raw)
        report = value.get('report', value)
        key = (report['stratum'], report['fold'], report['vibe'])
        if key in tasks:
            raise ValueError('Duplicate diagnostic fold')
        tasks.add(key)
        hashes[str(source)] = evaluation.digest(raw)
        for row in value['rows']:
            original = inputs[key[2]][row['recording']]
            if (original['score'] != row['truth'] or original['source'] != row['source']
                    or plan['assignments'][str(row['recording'])][key[0]] != key[1]):
                raise ValueError('Development diagnostic changed a target identity/truth')
        grouped[key[0], key[2]].extend(value['rows'])
    findings = {}
    for (stratum, vibe), rows in sorted(grouped.items()):
        metrics = evaluation.metrics(rows)
        artists = Counter(artist for row in inputs[vibe].values() for artist in row['artists'])
        genres = Counter(row['genre'] for row in inputs[vibe].values())
        def describe(row):
            meta = inputs[vibe][row['recording']]
            return {**{key: row[key] for key in ('recording', 'truth', 'baseline', 'candidate')},
                'source_family': row['source'].split('/', 1)[0], 'artists': meta['artists'],
                'genre': meta['genre'], 'album': meta['album'],
                'artist_original_counts': [artists[artist] for artist in meta['artists']],
                'genre_original_count': genres[meta['genre']] if meta['genre'] else None}
        ordered = {policy: sorted(rows, key=lambda row: (-row[policy], row['recording']))[:10]
                   for policy in ('baseline', 'candidate', 'truth')}
        findings[stratum + '/' + vibe] = {'metrics': metrics,
            'ranking_failed': metrics['baseline_ndcg10'] - metrics['candidate_ndcg10'] > evaluation.CRITERIA['ndcg10_drop'],
            'top10': {policy: [describe(row) for row in selected] for policy, selected in ordered.items()},
            'top10_source_counts': {policy: dict(Counter(row['source'].split('/', 1)[0] for row in selected))
                                    for policy, selected in ordered.items()},
            'source_metrics': {family: evaluation.metrics([row for row in rows if row['source'].split('/', 1)[0] == family])
                for family in sorted({row['source'].split('/', 1)[0] for row in rows})}}
    value = {'development_only': True, 'candidate_selected': False,
             'plan_sha256': evaluation.digest(path.read_bytes()), 'result_hashes': hashes,
             'historical_code': plan['code'], 'diagnostic_code': identity(),
             'task_count': len(tasks), 'findings': findings}
    evaluation.save_new(output, value)
    return {'output': str(output), 'task_count': len(tasks), 'failures': {
        key: {'source_counts': item['top10_source_counts'],
              'baseline_ndcg10': item['metrics']['baseline_ndcg10'],
              'candidate_ndcg10': item['metrics']['candidate_ndcg10']}
        for key, item in findings.items() if item['ranking_failed']}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'inventory', 'seal', 'run', 'summarize', 'diagnose'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--previous-labels', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    parser.add_argument('--partition', choices=('development', 'reserved'), default='development')
    parser.add_argument('--seal', type=Path)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if not args.db:
            parser.error('freeze requires an isolated --db')
        value = freeze(args.db, args.plan, previous_labels(args.previous_labels))
    elif args.action == 'inventory':
        value = inventory(load(args.plan), evaluation.digest(args.plan.read_bytes()))
    else:
        if not args.output:
            parser.error(args.action + ' requires --output')
        if args.action == 'seal':
            value = seal(args.plan, args.output)
        elif args.action == 'run':
            if args.stratum is None or args.fold is None or args.vibe is None:
                parser.error('run requires --stratum, --fold and --vibe')
            value = run(args.plan, args.output, args.stratum, args.fold, args.vibe, args.partition, args.seal)
        elif args.action == 'summarize':
            value = summarize(args.plan, args.result, args.output)
        else:
            value = diagnose(args.plan, args.result, args.output)
    print(json.dumps(value, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
