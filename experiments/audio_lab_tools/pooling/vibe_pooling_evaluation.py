"""Frozen original-only heldouts for #819; never fit against the live database.

freeze reads a consistent source via its repository. run uses only the frozen
JSON, hiding entire recordings and connected artist credits across all channels.
Each fold/vibe result is independent and resumable. No candidate is promoted by
this tool, and missing statistical/resource evidence is never a passing gate.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import resource
import sys
import time

import numpy as np

from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec import home_vibe, musical_preparation, vibe_joint_model as joint, vibe_prediction as baseline
from yt_playlist.rec import vibe_evaluation

ROOT = Path(__file__).resolve().parents[3]
CODE = ('src/yt_playlist/rec/vibe_joint_model.py', 'src/yt_playlist/rec/vibe_prediction.py',
        'src/yt_playlist/rec/vibe_joint_common.py',
        'src/yt_playlist/rec/vibe_joint_calibrated.py', 'src/yt_playlist/rec/vibe_joint_preparation.py',
        'src/yt_playlist/rec/musical_profiles.py', 'src/yt_playlist/rec/home_vibe.py',
        'src/yt_playlist/rec/musical_preparation.py', 'src/yt_playlist/rec/vibe_preparation.py',
        'src/yt_playlist/repos/musical_profiles.py', 'src/yt_playlist/repos/prepared_content.py',
        'src/yt_playlist/repos/rec_query.py', 'src/yt_playlist/repos/discovery.py',
        'src/yt_playlist/repos/vibe_models.py', 'src/yt_playlist/core/vibe_model_files.py',
        'src/yt_playlist/core/vibe_model_queries.py',
        'src/yt_playlist/core/vibe_manifest.py', 'src/yt_playlist/controllers/vibe_models.py',
        'src/yt_playlist/controllers/musical_profiles.py', 'src/yt_playlist/core/profile_files.py',
        'src/yt_playlist/core/audio_metadata_files.py', 'src/yt_playlist/core/metadata_native.py',
        'src/yt_playlist/core/preparation/metadata_destinations.py',
        'src/yt_playlist/repos/vibe_evaluation.py',
        'src/yt_playlist/controllers/vibe_evaluation.py', 'src/yt_playlist/rec/vibe_evaluation.py',
        'src/yt_playlist/controllers/prepared_content.py', 'src/yt_playlist/rec/prepared_content_inputs.py',
        'src/yt_playlist/controllers/genres.py',
        'src/yt_playlist/core/audio_vibe_contract.py',
        'experiments/audio_lab_tools/pooling/vibe_pooling_evaluation.py')
CRITERIA = {'macro_mae_gain': .05, 'stratum_mae_regression': .02, 'ndcg10_drop': .01,
            'coverage90': [.85, .95], 'minimum_artists': 5, 'minimum_recordings_per_vibe_stratum': 20,
            'bootstrap_repetitions': 1000, 'folds': 5}
PROFILE_MASK = ('production profile preparation with artist/release source descriptions admitted before '
                'source slots and byte budgets, bounded to 4096 link identities per recording; '
                'categorical genre choices retained; '
                'all recording-source observations and numeric selected/override fields removed')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def code_identity():
    return {name:digest((ROOT / name).read_bytes()) for name in CODE}


def save_new(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def assignments(rows, folds=5):
    """Connected collaboration credits stay wholly outside unseen-artist fits."""
    parents = {}
    def root(artist):
        parents.setdefault(artist, artist)
        while parents[artist] != artist:
            parents[artist] = parents[parents[artist]]
            artist = parents[artist]
        return artist
    for row in rows:
        artists = sorted(row['artists'])
        for artist in artists:
            parents[root(artist)] = root(artists[0])
    result = {}
    for row in rows:
        cluster = 'artist:' + str(root(row['artists'][0])) if row['artists'] else 'recording:' + str(row['recording'])
        recording = str(row['recording'])
        value = {'cluster': cluster, 'known': int(digest(recording.encode()), 16) % folds,
                 'unseen': int(digest(cluster.encode()), 16) % folds}
        previous = result.setdefault(recording, value)
        if previous != value:
            raise ValueError('Conditioning metadata differs across a recording\'s channels')
    return result


def freeze(database, destination, humans=None):
    # Only the isolated capture opens the source; fitting runs later from JSON.
    snapshot = vibe_evaluation.evaluation_snapshot(database, (*home_vibe.PRESETS, *INTENSITY_CHANNELS))
    rows = [row for values in snapshot['rows'].values() for row in values]
    labels = json.loads(humans.read_text()) if humans else []
    if any(set(row) != {'recording', 'vibe', 'score'} or row['vibe'] not in home_vibe.PRESETS
           or type(row['recording']) is not int or row['recording'] < 1
           or type(row['score']) not in (int,float) or not 0 <= row['score'] <= 1 for row in labels):
        raise ValueError('Human labels require canonical recording, one of six vibes, and score in [0,1]')
    if len({(row['recording'],row['vibe']) for row in labels}) != len(labels):
        raise ValueError('Duplicate human recording/vibe targets')
    originals = {(row['recording'],vibe) for vibe,values in snapshot['rows'].items() for row in values}
    plan = {**snapshot, 'format': 2, 'criteria': CRITERIA, 'candidate': joint.configuration(),
            'code': code_identity(), 'assignments': assignments(rows), 'human_targets': labels,
            'human_source_sha256': digest(humans.read_bytes()) if humans else None,
            'human_targets_without_original_conditioning': [row for row in labels if (row['recording'],row['vibe']) not in originals],
            'target_mask': 'all original measurements, providers, uploads and channels of each held-out recording',
            'profile_mask': PROFILE_MASK,
            'baseline_fallback': 'production musical_preparation.recordings and home_vibe.heuristic_affinity on frozen masked inputs',
            'bias': 'measurement targets share classifier bias; held-out humans reported separately'}
    save_new(destination, plan)
    return {'plan': str(destination), 'sha256': digest(destination.read_bytes()),
            'recordings': len(plan['assignments']), 'originals': len(rows)}


def load_plan(path):
    plan = json.loads(path.read_text())
    if plan.get('format') != 2 or plan.get('profile_mask') != PROFILE_MASK:
        raise ValueError('Frozen plan lacks current masked profile conditioning; freeze a separate plan')
    if plan['code'] != code_identity() or plan['criteria'] != CRITERIA or plan['candidate'] != joint.configuration():
        raise ValueError('Frozen code, configuration or criteria changed; freeze a separate plan')
    if set(plan['profile_inputs']) != set(plan['assignments']):
        raise ValueError('Frozen profiles must cover every evaluated recording')
    for value in plan['profile_inputs'].values():
        if (set(value['overrides']) - {'genre'}
                or any(source['scope'] not in {'artist', 'release'} for source in value['sources'])
                or any(features.get('audio') for features in value['content'].values() if features is not None)):
            raise ValueError('Target observations leaked into frozen profile conditioning')
    return plan


def masked_profile(value):
    """Use the production preparation path with detached, already masked inputs."""
    raw = json.dumps(value['content'], sort_keys=True, separators=(',', ':'), allow_nan=False)
    rows = [('target', 0, raw, None, True)]
    inputs = {'target': (value['sources'], value['overrides'], value['truncated'])}
    _key, payload, _version, _digest = musical_preparation.masked_recordings(None, rows, inputs=inputs)[0]
    return json.loads(payload) if payload is not None else None


def run(path, output, stratum, fold, vibe, *, plan_loader=load_plan):
    plan, started = plan_loader(path), time.monotonic()
    allocation, originals = plan['assignments'], plan['rows'][vibe]
    hidden = {int(key) for key, value in allocation.items() if value[stratum] == fold}
    human_ids = {row['recording'] for row in plan['human_targets']}
    prepared = [{**row, 'metadata_prior': home_vibe.heuristic_affinity(
        profile, vibe) if (profile := masked_profile(plan['profile_inputs'][str(row['recording'])])) is not None else .5}
        for row in originals]
    train = [row for row in prepared if row['recording'] not in hidden | human_ids]
    target = [row for row in originals if row['recording'] in hidden]
    # Human targets are never observations; their original measurements remain
    # hidden even in folds where another song is the measurement target.
    if {row['recording'] for row in train}.intersection(hidden | human_ids):
        raise ValueError('Held-out original leaked into training')
    result = {'plan_sha256': digest(path.read_bytes()), 'stratum': stratum, 'fold': fold, 'vibe': vibe,
              'training_recordings': len(train), 'target_recordings': len(target),
              'unscored_recordings': [], 'rows': []}
    try:
        fit_started = time.monotonic()
        with work_budget(10):
            value = joint.fit(train, vibe)
        fit_seconds = time.monotonic() - fit_started
        posterior = joint.Posterior(value) if value else None
    except WorkBudgetExceeded as exc:
        result.update(outcome='deferred', reason=str(exc))
        save_new(output, result)
        return result
    stats = {}
    for row in train:
        artist = json.dumps(row['artists'], separators=(',', ':')) if row['artists'] else None
        for group in baseline.groups(artist, row['genre']):
            stats[group] = baseline.adjust(stats.get(group, {}), {'fits': {vibe: row}}, 1)
    train_artists = {artist for row in train for artist in row['artists']}
    humans = {(row['recording'],row['vibe']):row['score'] for row in plan['human_targets']}
    for row in target:
        known = bool(train_artists.intersection(row['artists']))
        if not row['artists']:
            continue  # Missing artist metadata is not unseen-artist evidence.
        if stratum == 'known' and not known:
            continue
        if stratum == 'unseen' and known:
            raise ValueError('Artist or collaborator leaked across unseen-artist fold')
        artist = json.dumps(row['artists'], separators=(',', ':')) if row['artists'] else None
        profile = masked_profile(plan['profile_inputs'][str(row['recording'])])
        if profile is None:
            result['unscored_recordings'].append({'recording': row['recording'],
                'reason': 'no production profile after target observations are masked'})
            continue
        fallback = home_vibe.heuristic_affinity(profile, vibe)
        old = baseline.resolve({vibe:fallback}, None, [(key,stats[key]) for key in baseline.groups(artist,row['genre'])
                                                     if key in stats])['fits'][vibe]['score']
        estimate = posterior.predict(row['artists'], row['genre'], album=row['album'], recording=row['recording'],
                                     metadata_prior=fallback) if posterior else None
        score = estimate['score'] if estimate else old
        variance = estimate['new_track_variance'] if estimate else None
        interval = (estimate.get('interval90') or [max(0.,score-1.6448536269514722*math.sqrt(variance)),
                    min(1.,score+1.6448536269514722*math.sqrt(variance))]) if variance is not None else None
        result['rows'].append({'recording': row['recording'], 'cluster': allocation[str(row['recording'])]['cluster'],
            'source': row['source'], 'truth': row['score'], 'baseline': old, 'candidate': score,
            'interval90': interval, 'mean_variance': estimate['mean_variance'] if estimate else None,
            'new_track_variance': variance, 'human_truth': humans.get((row['recording'],vibe))})
    result.update(outcome='complete', wall_seconds=time.monotonic()-started, fit_seconds=fit_seconds,
        platform=sys.platform,
        peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
        accounted_fit_bytes=value['diagnostics']['accounted_memory_bytes'] if value else 0,
        training_artist_counts=dict(Counter(artist for row in train for artist in row['artists'])),
        source_counts=dict(Counter(row['source'] for row in train)),
        diagnostics=value['diagnostics'] if value else None)
    save_new(output, result)
    return {key:item for key,item in result.items() if key != 'rows'}


def ndcg(rows, field, truth='truth'):
    def dcg(values):
        return sum((2 ** row[truth] - 1) / math.log2(index + 2) for index,row in enumerate(values[:10]))
    ideal = dcg(sorted(rows, key=lambda row:row[truth], reverse=True))
    return dcg(sorted(rows, key=lambda row:(-row[field], row['recording']))) / ideal if ideal else 1.


def metrics(rows):
    if not rows:
        return None
    intervals = [row for row in rows if row['interval90'] is not None]
    return {'recordings': len(rows), 'artist_clusters': len({row['cluster'] for row in rows}),
            'baseline_mae': float(np.mean([abs(row['truth']-row['baseline']) for row in rows])),
            'candidate_mae': float(np.mean([abs(row['truth']-row['candidate']) for row in rows])),
            'baseline_ndcg10': ndcg(rows,'baseline'), 'candidate_ndcg10': ndcg(rows,'candidate'),
            'interval_recordings': len(intervals),
            'coverage90': float(np.mean([row['interval90'][0] <= row['truth'] <= row['interval90'][1]
                                        for row in intervals])) if intervals else None,
            'interval_width': float(np.mean([row['interval90'][1]-row['interval90'][0] for row in intervals])) if intervals else None}


def summarize(path, results, output):
    plan = load_plan(path)
    tasks, grouped = {}, defaultdict(list)
    for source in results:
        report = json.loads(source.read_text())
        if report['plan_sha256'] != digest(path.read_bytes()):
            raise ValueError('Result belongs to another plan')
        key = (report['stratum'],report['fold'],report['vibe'])
        if key in tasks:
            raise ValueError('Duplicate fold/vibe result')
        tasks[key] = report
        if report['outcome'] == 'complete':
            grouped[report['stratum'],report['vibe']] += report['rows']
    expected = {(stratum,fold,vibe) for stratum in ('known','unseen') for fold in range(CRITERIA['folds'])
                for vibe in home_vibe.PRESETS}
    complete = set(tasks) == expected and all(row['outcome'] == 'complete' for row in tasks.values())
    measures = {stratum:{vibe:metrics(grouped[stratum,vibe]) for vibe in home_vibe.PRESETS}
                for stratum in ('known','unseen')}
    adequate = complete and all(value and value['recordings'] >= CRITERIA['minimum_recordings_per_vibe_stratum']
        and value['artist_clusters'] >= CRITERIA['minimum_artists']
        and value['interval_recordings'] == value['recordings'] for values in measures.values() for value in values.values())
    # Bootstrap whole connected artist clusters, with the same sampled clusters
    # for both policies and every vibe/stratum. No song-level pseudo-replicates.
    clusters = sorted({row['cluster'] for rows in grouped.values() for row in rows})
    rng, gains, intervals = np.random.default_rng(819), [], defaultdict(lambda:defaultdict(list))
    if adequate:
        for _ in range(CRITERIA['bootstrap_repetitions']):
            weights = Counter(rng.choice(clusters, len(clusters), replace=True))
            sampled = {key:[row for row in rows for _ in range(weights[row['cluster']])]
                       for key,rows in grouped.items()}
            for key,rows in sampled.items():
                measure = metrics(rows)
                if measure:
                    for field in ('baseline_mae','candidate_mae','baseline_ndcg10','candidate_ndcg10','coverage90','interval_width'):
                        intervals[key][field].append(measure[field])
            per_vibe = []
            for vibe in home_vibe.PRESETS:
                values = sampled['known',vibe] + sampled['unseen',vibe]
                if values:
                    per_vibe.append(np.mean([abs(row['truth']-row['baseline'])-abs(row['truth']-row['candidate']) for row in values]))
            if len(per_vibe) == len(home_vibe.PRESETS):
                gains.append(float(np.mean(per_vibe)))
    combined = [metrics(grouped['known',vibe] + grouped['unseen',vibe]) for vibe in home_vibe.PRESETS]
    statistical = False
    if adequate and gains:
        baseline_error = np.mean([value['baseline_mae'] for value in combined])
        candidate_error = np.mean([value['candidate_mae'] for value in combined])
        statistical = bool(candidate_error <= baseline_error * (1-CRITERIA['macro_mae_gain'])
            and np.quantile(gains,.025) > 0 and all(
                np.mean([value['candidate_mae'] for value in values.values()]) <=
                np.mean([value['baseline_mae'] for value in values.values()]) * (1+CRITERIA['stratum_mae_regression'])
                and all(value['candidate_ndcg10'] >= value['baseline_ndcg10']-CRITERIA['ndcg10_drop']
                        and CRITERIA['coverage90'][0] <= value['coverage90'] <= CRITERIA['coverage90'][1]
                        for value in values.values()) for values in measures.values()))
    human = {stratum:{vibe:metrics([{**row,'truth':row['human_truth']} for row in grouped[stratum,vibe]
                                   if row['human_truth'] is not None]) for vibe in home_vibe.PRESETS}
             for stratum in ('known','unseen')}
    sources = {stratum:{vibe:{source:metrics([row for row in grouped[stratum,vibe] if row['source'] == source])
                for source in sorted({row['source'] for row in grouped[stratum,vibe]})}
                for vibe in home_vibe.PRESETS} for stratum in ('known','unseen')}
    report = {'plan_sha256': digest(path.read_bytes()), 'complete': complete, 'adequate': adequate,
              'measurement_metrics': measures, 'human_metrics': human,
              'source_metrics': sources,
              'artist_cluster_intervals95': {stratum:{vibe:{field:list(map(float,np.quantile(values,[.025,.975])))
                  for field,values in intervals[stratum,vibe].items()} for vibe in home_vibe.PRESETS}
                  for stratum in ('known','unseen')},
              'human_targets_without_original_conditioning': plan['human_targets_without_original_conditioning'],
              'masked_profile_gaps': [{'stratum': row['stratum'], 'fold': row['fold'], 'vibe': row['vibe'], **gap}
                  for row in tasks.values() for gap in row.get('unscored_recordings', ())],
              'paired_macro_mae_gain95': list(map(float,np.quantile(gains,[.025,.975]))) if gains else None,
              'statistical_gates_pass': statistical, 'candidate_selected': False,
              'remaining_gates': ['actual refresh SQL/index/FAO bytes and bounded retention',
                  'stale/interrupted/corrupt/backup lifecycle acceptance',
                  'paired foreground p95 and first-useful-output timings',
                  'review of classifier bias, source reliability and plug-in uncertainty'],
              'limits': ['No human labels enter a fit.', 'Source reliability is observed error, not proof of independent measurement accuracy.',
                         'Human targets lacking current original conditioning are reported as a gap, not scored.',
                         'Both policies use the same frozen production profile inputs with recording observations and numeric fields masked.',
                         'Timing/RSS from isolated folds does not establish production foreground latency.']}
    save_new(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze','run','summarize'))
    parser.add_argument('--db', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--human-labels', type=Path)
    parser.add_argument('--stratum', choices=('known','unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if not args.db:
            parser.error('freeze requires --db; it is opened read-only by the repository')
        result = freeze(args.db, args.plan, args.human_labels)
    elif args.action == 'run':
        if not args.output or args.stratum is None or args.fold is None or args.vibe is None:
            parser.error('run requires --output, --stratum, --fold and --vibe')
        result = run(args.plan, args.output, args.stratum, args.fold, args.vibe)
    else:
        if not args.output:
            parser.error('summarize requires --output and --result files')
        result = summarize(args.plan, args.result, args.output)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
