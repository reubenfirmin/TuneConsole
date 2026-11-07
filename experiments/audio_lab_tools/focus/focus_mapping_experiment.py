"""Freeze, execute and audit #615's nested Focus mapping comparison."""
import argparse
from collections import Counter
from pathlib import Path
import json
import re
import resource
import shutil
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from . import focus_mapping as models
from ..studies import individual_protocol as protocol
from ..datasets.external_annotations import split_half
from ..support.external_experiment import DATA, LAB, digest, save, stamp
from ..training.external_training import folds
from .focus_experiment import arrays, check_receipt, head_predictions, inside, load, runtime
from ..studies.individual_experiment import compact, receipt
from ..relax.relax_experiment import global_groups, validate
from ..uplift.uplift_experiment import padded

EXPERIMENTS = LAB.parent
PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
FOCUS = LAB / 'local/individual-20261030/focus-1'
DEFAULT = LAB / 'local/focus-615-20261030/round-1'
LOCAL = LAB / 'local/focus-615-20261030/local-1'
VOCALS = ['Instrument_-_Male_Lead_Vocals', 'Instrument_-_Female_Lead_Vocals',
          'Instrument_-_Backing_vocals', 'Instrument_-_Choir']


def annotations(rows, ref):
    base = DATA / 'cal500/annotations-source'
    ref(base / 'README_CAL500.txt')
    ratings, vocal, support = [], [], []
    for row in rows:
        responses, voices = [], []
        for path in sorted((base / 'annotations').glob(row['id'].removeprefix('cal500:') + '_[0-9][0-9].txt')):
            parsed = dict(re.findall(r'^([^=]+?) = "([^"]*)"', ref(path).read_text(), re.M))
            raw = np.array([int(parsed[k]) for k in models.ATTRIBUTES])
            if np.any(raw < 1) or np.any(raw > 5):
                raise ValueError('Unexpected native ordinal encoding')
            responses.append([*((raw - 1) / 4), *(float(parsed.get(k) == 'Yes') for k in models.TARGETS)])
            states = [parsed.get(k) for k in VOCALS]
            voices.append(1. if any(v in ('Present', 'Prominent') for v in states)
                          else 0. if all(v == 'None' for v in states) else np.nan)
        if len(responses) != row['rater_count'] or len(responses) < 2:
            raise ValueError('Annotation support changed: ' + row['id'])
        ratings.append(np.asarray(responses))
        known = np.isfinite(voices)
        vocal.append(float(np.asarray(voices)[known].mean()) if known.any() else np.nan)
        support.append(int(known.sum()))
    mean = np.array([r.mean(axis=0) for r in ratings])
    return mean[:, :10], mean[:, 10:], np.array(vocal), np.array(support), {
        'rows': len(rows), 'responses': sum(map(len, ratings)), 'attributes': models.ATTRIBUTES,
        'rater_min_median_max': [min(map(len, ratings)), float(np.median(list(map(len, ratings)))), max(map(len, ratings))],
        'activity_nonzero_rows': dict(zip(models.TARGETS, np.count_nonzero(mean[:, 10:], axis=0).tolist(), strict=True)),
        'either_activity_nonzero': int(np.any(mean[:, 10:] > 0, axis=1).sum()),
        'both_activities_nonzero': int(np.all(mean[:, 10:] > 0, axis=1).sum()),
        'activity_means': mean[:, 10:].mean(axis=0).tolist(),
        'within_recording_listener_variance': np.mean([r.var(axis=0) for r in ratings], axis=0).tolist(),
        'split_half': split_half(ratings, [*models.ATTRIBUTES, *models.TARGETS]),
        'vocal_known_responses': int(sum(support)), 'vocal_unknown_responses': sum(map(len, ratings)) - sum(support),
        'encoding_limit': 'Selected raw emotion fields use 1–5 although README describes three levels; normalized (raw-1)/4.',
        'annotation_limit': 'Whole-song listener labels paired with one ten-second excerpt; no excerpt distraction labels.'}


def code_paths():
    paths = {Path(m.__file__).resolve() for name, m in list(sys.modules.items())
             if (name == 'experiments.audio_lab_tools' or name.startswith('experiments.audio_lab_tools.'))
             and getattr(m, '__file__', None)}
    paths.update((EXPERIMENTS / 'run.py', LAB / 'FOCUS_MAPPING_PLAN.md', Path(__file__).with_name('test_focus_mapping.py')))
    return {str(p.relative_to(EXPERIMENTS)): p for p in sorted(paths)}


def freeze(directory, local):
    from ..studies.individual_experiment import validate as previous_validate
    from .focus_experiment import validate as focus_validate
    if directory.exists():
        raise ValueError('Preserve prior attempts; choose a new directory')
    previous_validate(PREVIOUS)
    prior = focus_validate(FOCUS)
    check_receipt(FOCUS, 'evaluation.receipt.json')
    check_receipt(local, 'receipt.json', False)
    references = {}

    def ref(path):
        path = inside(path)
        references[str(path)] = digest(path)
        return path

    for name in ('plan.json', 'evaluation.receipt.json'):
        ref(PREVIOUS / name)
        ref(FOCUS / name)
    old, legacy = arrays(ref(FOCUS / 'inputs.npz')), arrays(ref(FOCUS / 'oof.npz'))
    focus_meta = load(ref(FOCUS / 'metadata.json'))
    ref(FOCUS / 'checkpoint.json')
    production = EXPERIMENTS.parent / 'src/yt_playlist/rec/data/emotify-vibes.json'
    if load(production) != load(FOCUS / 'checkpoint.json'):
        raise ValueError('Shared production model changed; refresh controls')
    references[str(production)] = digest(production)
    ref(local / 'receipt.json')
    for name in load(local / 'receipt.json')['outputs']:
        ref(local / name)
    local_rows = load(local / 'labels.json')['rows']
    metadata, original, rows, boundaries = {}, {}, [], {}
    for source in models.SOURCES:
        metadata[source] = load(ref(PREVIOUS / f'inputs/{source}.json'))
        original[source] = arrays(ref(PREVIOUS / f'inputs/{source}.npz'))
        source_rows = metadata[source]['rows']
        boundaries[source] = slice(len(rows), len(rows) + len(source_rows))
        rows.extend(source_rows)
    groups = global_groups([*rows, *local_rows])
    protected = set(groups[len(rows):])
    semantic, activity, vocal, support, audit = annotations(metadata[models.CAL]['rows'], ref)
    cal_ids = [r['id'] for r in metadata[models.CAL]['rows']]
    if cal_ids != [r['id'] for r in focus_meta['rows']] or not np.allclose(old['y'], activity, atol=1e-12, rtol=0):
        raise ValueError('Original Focus targets/identities changed')
    for key in ('embedding', 'browser'):
        if not np.array_equal(old[key], original[models.CAL][key]):
            raise ValueError('Archived Focus features differ')
    directory.mkdir(parents=True)
    (directory / 'inputs').mkdir()
    inventory = {}
    for source in models.SOURCES:
        d, meta = original[source], metadata[source]
        source_rows, names = meta['rows'], meta['target_names']
        source_groups = groups[boundaries[source]]
        keep = np.array([g not in protected for g in source_groups])
        offsets = d.get('offsets', np.arange(len(source_rows) + 1))
        chunks, counts = padded(d['embedding'], offsets)
        targets = models.TARGETS if source == models.CAL else models.NATIVE[source]
        out = {'x': protocol.mean_chunks(d['embedding'], offsets)[keep], 'chunks': chunks[keep], 'counts': counts[keep],
               'y': d['y'][keep][:, [names.index(k) for k in targets]], 'groups': source_groups[keep],
               'ids': np.array([r['id'] for r in source_rows])[keep]}
        if source == models.CAL:
            out.update(semantic=semantic[keep], vocal=vocal[keep], vocal_support=support[keep], common=old['common'][keep],
                       browser=padded(d['browser'], offsets)[0][keep])
            for key in ('checkpoint', 'refresh_calibration', 'refresh_both', 'browser_shared', 'prior_native'):
                out['control__' + key] = old[key][keep]
            for key, value in legacy.items():
                out['legacy__' + key] = value[keep]
            out['outer'] = folds(out['groups'], count=5, seed=models.SEED)
            protocol.group_guard(out['groups'], out['outer'])
            audit['old_fold_crossing_components'] = [int(g) for g in set(source_groups)
                if len(set(old['outer'][source_groups == g])) > 1]
            audit['common_rows'] = int(out['common'].sum())
            audit['outer_sizes'] = [int((out['outer'] == f).sum()) for f in range(5)]
        np.savez_compressed(directory / f'inputs/{source}.npz', **out)
        save(directory / f'inputs/{source}.json', {**meta, 'rows': [r for r, k in zip(source_rows, keep, strict=True) if k]})
        inventory[source] = {'supplied_rows': len(source_rows), 'retained_rows': int(keep.sum()), 'groups': len(set(out['groups'])),
            'fitted_labels': models.NATIVE[source], 'chunks': int(counts[keep].sum()),
            'excluded_ids': [r['id'] for r, k in zip(source_rows, keep, strict=True) if not k],
            'missing_artist_rows': sum(not r.get('artist') for r, k in zip(source_rows, keep, strict=True) if k),
            'annotation_units': dict(Counter(r.get('annotation_unit', 'unknown') for r, k in zip(source_rows, keep, strict=True) if k))}
    save(directory / 'inputs/audit.json', {'sources': inventory, 'cal500': audit,
        'local': {'rows': len(local_rows), 'positives': dict(Counter(v for r in local_rows for v in r['labels'])),
                  'new_since_570': sum(not r['prior_inspected'] for r in local_rows),
                  'labels_changed_since_570': sum(r['label_changed_since_570'] for r in local_rows),
                  'skipped': load(local / 'labels.json')['skipped']},
        'identity_limit': 'Conservative artist/recording/audio/title/declared-group components; missing identities and unknown encoder exposure remain.'})
    paths = code_paths()
    for name, path in paths.items():
        destination = directory / 'source' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    plan = {'created_at': stamp(), 'ticket': 615, 'predecessor': 576, 'runtime': runtime(), 'seed': models.SEED,
        'local': str(local), 'references': references, 'code': {n: digest(p) for n, p in paths.items()},
        'inputs': {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').iterdir()},
        'embedding_signature': prior['embedding_signature'], 'procedures': list(models.METHODS), 'bootstrap_draws': 1000,
        'acceptance': {'browser_shared_gain': .02, 'browser_latest_gain': .01, 'positive_shared_interval': True,
                       'maximum_ndcg_loss': .02, 'maximum_lift_loss': .01,
                       'maximum_native_ndcg_loss': .03, 'maximum_native_lift_loss': .02, 'incremental_direct_gain': .01},
        'budgets': {'bytes': 1048576, 'warm_three_ms': 100, 'rss_bytes': 1073741824},
        'exposure': 'Previously inspected populations; new grouped folds do not create fresh confirmation.',
        'terms': 'Retain original source terms, including ManyMusic NC/SA and Memo CC BY 4.0; research artifacts only.'}
    save(directory / 'plan.json', plan)
    validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
                      'cal500': audit, 'local_rows': len(local_rows)}), flush=True)


def datasets(directory):
    return {s: arrays(directory / f'inputs/{s}.npz') for s in models.SOURCES}


def fit_stage(directory, stage, training, validation=None):
    if (directory / (stage + '.receipt.json')).exists():
        check_receipt(directory, stage + '.receipt.json')
        return
    start = time.monotonic()
    print(json.dumps({'stage': stage, 'training_rows': {s: len(d['y']) for s, d in training.items()}}), flush=True)
    model, trace = models.fit(training)
    result = {}
    if validation is not None:
        if any(set(d['groups']) & set(validation['groups']) for d in training.values()):
            raise ValueError('Outer validation component entered training')
        result['ids'] = validation['ids']
        result['observed_attributes'] = head_predictions(model['observed'], validation['semantic'])
        result['training_mean'] = np.tile(training[models.CAL]['y'].mean(axis=0), (len(validation['y']), 1))
        for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
            scores, attrs = models.predict(model, validation[key], validation['counts'])
            result.update({prep + '__' + k: v for k, v in scores.items()})
            result[prep + '__native_attributes'] = attrs
    paths = [directory / f'models/{stage}.json', directory / f'{stage}.trace.json', directory / f'{stage}.npz']
    compact(paths[0], model)
    compact(paths[1], trace)
    np.savez_compressed(paths[2], **result)
    receipt(directory, stage, paths)
    print(json.dumps({'completed': stage, 'seconds': time.monotonic() - start}), flush=True)


def fit_all(directory):
    data = datasets(directory)
    d = data[models.CAL]
    for fold in range(5):
        valid = d['outer'] == fold
        fit_stage(directory, 'outer-' + str(fold), models.protect(data, d['groups'][valid]), models.subset(d, valid))
    fit_stage(directory, 'full', data)


def metric_report(d, target, scores, draws):
    pairs = [(m, c) for m in models.METHODS for c in ('shared', 'latest', 'direct') if c in scores and c != m]
    pairs += [('observed_attributes', 'cal_only'), ('cal_only', 'training_mean'),
              ('attributes', 'cal_only'), ('attributes', 'external_only')]
    return {'rows': len(target), 'groups': len(set(d['groups'])),
        'metrics': {k: {**protocol.metrics(target, v, d['outer']), 'mae': float(np.abs(v - target).mean())} for k, v in scores.items()},
        'paired': protocol.paired(target, scores, d['outer'], d['groups'], pairs, draws)}


def contrasts(d, scores):
    masks = {'mostly_vocal': d['vocal'] >= .75, 'mostly_nonvocal': d['vocal'] <= .25,
             'vocal_unknown': ~np.isfinite(d['vocal']), 'high_energy': d['semantic'][:, 0] >= .65,
             'low_energy': d['semantic'][:, 0] <= .35, 'reading_without_studying': (d['y'][:, 0] > 0) & (d['y'][:, 1] == 0),
             'studying_without_reading': (d['y'][:, 1] > 0) & (d['y'][:, 0] == 0)}
    result = {}
    for name, mask in masks.items():
        observed = d['y'][mask].mean(axis=1)
        result[name] = {'rows': int(mask.sum()), 'ids': d['ids'][mask].tolist(),
            'reference_mean': float(observed.mean()) if mask.any() else None,
            'scores': {k: {'mean': float(v[mask].mean()), 'mse': float(((v[mask] - observed) ** 2).mean())}
                       for k, v in scores.items()} if mask.any() else {}}
    return result


def local_resources(directory, plan):
    from yt_playlist.core import audio_embedding, sample_files
    local, model = Path(plan['local']), load(directory / 'models/full.json')
    exports, paths, resources = {}, [], {}
    for method in models.METHODS:
        exports[method] = models.export(model, method, digest(directory / 'plan.json'), plan['embedding_signature'])
        path = directory / f'exports/{method}.json'
        compact(path, exports[method])
        paths.append(path)
    records, maxima, replay_error = [], {m: [] for m in models.METHODS}, {m: 0. for m in models.METHODS}
    for row in load(local / 'labels.json')['rows']:
        samples = [sample_files.read(local / name) for name in row['sample_files']]
        if any(audio_embedding.signature(s) != plan['embedding_signature'] for s in samples):
            raise ValueError('Local feature identity changed')
        x = np.array([s['embedding']['values'] for s in samples])
        for expert in model['experts'].values():
            if np.max(np.abs(head_predictions(expert['heads'], x) - head_predictions(expert['heads'], x, True))) > 1e-12:
                raise ValueError('Per-excerpt native head replay mismatch')
        stages = {}
        for stage, xx in (('first', x[:1]), ('three', x[:3]), ('all', x)):
            chunks, counts = xx[None], np.array([len(xx)])
            reference, attributes = models.predict(model, chunks, counts)
            independent_attributes = models.attribute_features(model['experts'], chunks, counts, True)
            if np.max(np.abs(attributes - independent_attributes)) > 1e-12:
                raise ValueError('Native attribute replay mismatch')
            stages[stage] = {}
            for method, artifact in exports.items():
                value = models.exported_predict(artifact, chunks, counts)
                independent = models.exported_predict(artifact, chunks, counts, True)
                replay_error[method] = max(replay_error[method], float(np.max(np.abs(value - independent))),
                                          float(np.max(np.abs(value - reference[method]))))
                stages[stage][method] = value[0].tolist()
        records.append({'id': row['id'], 'focus_positive': 'concentrate' in row['labels'], 'samples': len(x), 'scores': stages})
        if len(x) >= 3:
            for method, artifact in exports.items():
                start = time.perf_counter()
                models.exported_predict(artifact, x[:3][None], np.array([3]))
                maxima[method].append((time.perf_counter() - start) * 1000)
    for method, path in zip(models.METHODS, paths, strict=True):
        resource_row = {'bytes': path.stat().st_size, 'warm_three_max_ms': max(maxima[method]),
            'warm_three_median_ms': float(np.median(maxima[method])), 'replay_error': replay_error[method],
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
        resource_row['pass'] = resource_row['bytes'] <= plan['budgets']['bytes'] and resource_row['warm_three_max_ms'] <= plan['budgets']['warm_three_ms'] \
            and resource_row['peak_rss_bytes'] <= plan['budgets']['rss_bytes'] and replay_error[method] <= 1e-12
        resources[method] = resource_row
    return {'rows': len(records), 'focus_positives': sum(r['focus_positive'] for r in records), 'records': records,
            'limit': 'Positive-only supplementary replay, not accuracy or independent confirmation.'}, resources, paths


def decision(report, plan):
    result, rule = {}, plan['acceptance']
    for method in models.METHODS:
        reasons = []
        browser = report['outer']['browser']['focus']['paired']
        shared, latest = browser[method + '__vs__shared']['ndcg'], browser[method + '__vs__latest']['ndcg']
        if shared['difference'] < rule['browser_shared_gain']:
            reasons.append('Browser gain over shared is below 0.02.')
        if latest['difference'] < rule['browser_latest_gain']:
            reasons.append('Browser gain over refitted #576 is below 0.01.')
        if shared['conditional_95_percent_interval'][0] <= 0:
            reasons.append('Browser paired NDCG interval over shared includes zero.')
        if method != 'direct' and browser[method + '__vs__direct']['ndcg']['difference'] < rule['incremental_direct_gain']:
            reasons.append('Browser gain over direct is below 0.01.')
        for prep, tasks in report['outer'].items():
            for task, table in tasks.items():
                for control in ('shared', 'latest') if task == 'focus' else ('latest',):
                    pair = table['paired'][method + '__vs__' + control]
                    prefix = 'maximum_' if task == 'focus' else 'maximum_native_'
                    if pair['ndcg']['difference'] < -rule[prefix + 'ndcg_loss'] or pair['top_lift']['difference'] < -rule[prefix + 'lift_loss']:
                        reasons.append(f'Material regression: {prep}/{task} versus {control}.')
        if not report['resources'][method]['pass']:
            reasons.append('Replay/resource bounds failed.')
        result[method] = {'recommendation': 'retain' if reasons else 'candidate_for_review', 'reasons': reasons}
    return result


def evaluate(directory, plan):
    data = datasets(directory)[models.CAL]
    pooled = {}
    for fold in range(5):
        stage = 'outer-' + str(fold)
        check_receipt(directory, stage + '.receipt.json')
        values, mask = arrays(directory / (stage + '.npz')), data['outer'] == fold
        if not np.array_equal(values['ids'], data['ids'][mask]):
            raise ValueError('Prediction identity mismatch')
        for key, value in values.items():
            if key != 'ids':
                pooled.setdefault(key, np.empty((len(data['y']), value.shape[1])))[mask] = value
    d = models.subset(data, data['common'])
    pooled = {k: v[data['common']] for k, v in pooled.items()}
    saved = {'ids': d['ids'], 'groups': d['groups'], 'outer': d['outer'], 'observed': d['y'], **pooled}
    report = {'created_at': stamp(), 'outer': {}, 'contrasts': {}, 'native': {}, 'exposure': plan['exposure']}
    for prep in ('historical', 'browser'):
        native = {m: pooled[prep + '__' + m] for m in (*models.METHODS, 'cal_only', 'external_only')}
        native.update(latest=pooled[prep + '__latest_' + prep], observed_attributes=pooled['observed_attributes'], training_mean=pooled['training_mean'])
        report['outer'][prep] = {}
        for task, col in (('focus', None), ('reading', 0), ('studying', 1)):
            print(json.dumps({'evaluate': prep, 'task': task}), flush=True)
            reduce = (lambda x: x.mean(axis=1)) if col is None else (lambda x: x[:, col])
            scores = {k: reduce(v) for k, v in native.items()}
            scores['archived_prior'] = reduce(d['control__prior_native'])
            for k in ('historical_embedding', 'historical_combined', 'browser_embedding', 'acoustic_ridge'):
                scores['archived_' + k] = reduce(d['legacy__' + k])
            if task == 'focus':
                scores['shared'] = d['control__checkpoint' if prep == 'historical' else 'control__browser_shared']
                for k in ('refresh_calibration', 'refresh_both'):
                    scores['archived_' + k] = d['control__' + k]
                report['contrasts'][prep] = contrasts(d, scores)
            report['outer'][prep][task] = metric_report(d, reduce(d['y']), scores, plan['bootstrap_draws'])
            saved.update({prep + '__' + task + '__' + k: v for k, v in scores.items()})
        attrs = pooled[prep + '__native_attributes'][:, :10]
        report['native'][prep] = {k: {'mae': float(np.abs(attrs[:, i] - d['semantic'][:, i]).mean()),
                                     'mse': float(((attrs[:, i] - d['semantic'][:, i]) ** 2).mean())}
                                for i, k in enumerate(models.ATTRIBUTES)}
    check_receipt(directory, 'full.receipt.json')
    trace = load(directory / 'full.trace.json')
    report['native']['external_selection_diagnostics'] = {s: trace['final_native'][s] for s in models.SOURCES if s != models.CAL}
    report['local'], report['resources'], paths = local_resources(directory, plan)
    report['decisions'] = decision(report, plan)
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', **saved)
    receipt(directory, 'evaluation', [directory / 'report.json', directory / 'predictions.npz', *paths])
    print(json.dumps({'decisions': report['decisions'], 'resources': report['resources']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'fit', 'evaluate', 'run', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--local', type=Path, default=LOCAL)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.command == 'freeze':
            freeze(directory, inside(args.local))
            return
        plan = validate(directory, args.command != 'verify')
        if args.command in ('fit', 'run'):
            fit_all(directory)
        if args.command in ('evaluate', 'run'):
            evaluate(directory, plan)
        if args.command == 'verify':
            for stage in [*('outer-' + str(f) for f in range(5)), 'full', 'evaluation']:
                check_receipt(directory, stage + '.receipt.json')
            print(json.dumps({'verified': str(directory)}), flush=True)


if __name__ == '__main__':
    main()
