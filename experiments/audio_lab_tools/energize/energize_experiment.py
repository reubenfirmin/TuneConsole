"""Freeze and execute #616's activation, consensus and activity experiments."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import resource
import shutil
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from . import energize_consensus as models
from ..studies import individual_protocol as protocol
from ..datasets.external_annotations import split_half
from ..support.external_experiment import DATA, LAB, digest, save, stamp
from ..focus.focus_experiment import arrays, check_receipt, head_predictions, inside, load, runtime
from ..studies.individual_experiment import compact, receipt
from ..relax.relax_experiment import global_groups, validate
from ..uplift.uplift_experiment import padded

PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
DEFAULT = LAB / 'local/energize-616-20261030/round-1'
LOCAL = LAB / 'local/energize-616-20261030/local-1'
EXPERIMENTS = LAB.parent
PRIMARY = (models.SOUND, models.NAT, models.GEN, models.MEMO, models.CAL)
OMISSIONS = {'soundtracks': (models.SOUND,), 'manymusic': (models.NAT, models.GEN),
             'memo': (models.MEMO,), 'exercise': (models.CAL,)}
CAL_ATTRIBUTES = ['Song-High_Energy', 'Song-Heavy_Beat', 'Song-Fast_Tempo', 'Song-Very_Danceable',
                  'Emotion-Powerful_/_Strong', 'Emotion-Cheerful_/_Festive', 'Emotion-Arousing_/_Awakening']


def cal_annotations(rows, ref):
    base = DATA / 'cal500/annotations-source'
    ref(base / 'README_CAL500.txt')
    values, votes, annotations = [], [], []
    for row in rows:
        files = sorted((base / 'annotations').glob(row['id'].removeprefix('cal500:') + '_[0-9][0-9].txt'))
        ratings = []
        for path in files:
            parsed = dict(re.findall(r'^([^=]+?) = "([^"]*)"', ref(path).read_text(), re.M))
            native = np.array([int(parsed[k]) for k in CAL_ATTRIBUTES])
            if np.any(native < 1) or np.any(native > 5):
                raise ValueError('Unsupported CAL500 ordinal encoding')
            ratings.append([*((native - 1) / 4), float(parsed.get('Usage-Exercising') == 'Yes')])
        if len(ratings) < 2 or len(ratings) != row['rater_count']:
            raise ValueError('CAL500 response support changed: ' + row['id'])
        ratings = np.asarray(ratings)
        annotations.append(ratings)
        values.append(ratings[:, :-1].mean(axis=0))
        votes.append(ratings[:, -1].mean())
    return np.asarray(values), np.asarray(votes), {
        'attributes': CAL_ATTRIBUTES, 'normalization': '(raw ordinal - 1) / 4; listener mean',
        'raw_emotion_readme_discrepancy': 'README describes three emotion levels; all selected raw fields contain 1–5.',
        'rows': len(rows), 'responses': sum(len(v) for v in annotations),
        'rater_min_median_max': [min(map(len, annotations)), float(np.median(list(map(len, annotations)))), max(map(len, annotations))],
        'exercise_nonzero_rows': int(np.count_nonzero(votes)),
        'mean_within_recording_listener_variance': np.mean([v.var(axis=0) for v in annotations], axis=0).tolist(),
        'split_half': split_half(annotations, [*CAL_ATTRIBUTES, 'Usage-Exercising'])}


def code_paths():
    paths = {Path(m.__file__).resolve() for name, m in list(sys.modules.items())
             if (name == 'experiments.audio_lab_tools' or name.startswith('experiments.audio_lab_tools.'))
             and getattr(m, '__file__', None)}
    paths.update((EXPERIMENTS / 'run.py',
                  Path(__file__).with_name('test_energize_consensus.py')))
    return {str(p.relative_to(EXPERIMENTS)): p for p in sorted(paths)}


def freeze(directory, local):
    from ..studies.individual_experiment import validate as previous_validate
    if directory.exists():
        raise ValueError('Preserve prior attempts; choose a new directory')
    previous = previous_validate(PREVIOUS)
    check_receipt(PREVIOUS, 'evaluation.receipt.json')
    check_receipt(local, 'receipt.json', False)
    references = {}

    def ref(path):
        path = inside(path)
        references[str(path)] = digest(path)
        return path

    for name in ('plan.json', 'evaluation.receipt.json', 'primary.predictions.npz', 'primary.report.json', 'inputs/checkpoint.json'):
        ref(PREVIOUS / name)
    # The other specialists do not alter Energize. Verify the retained shared
    # checkpoint is exactly the current production model before reusing scores.
    production = EXPERIMENTS.parent / 'src/yt_playlist/rec/data/emotify-vibes.json'
    if load(production) != load(PREVIOUS / 'inputs/checkpoint.json'):
        raise ValueError('Production shared model changed; refresh the baseline')
    references[str(production)] = digest(production)
    ref(local / 'receipt.json')
    for name in load(local / 'receipt.json')['outputs']:
        ref(local / name)
    local_rows = load(local / 'labels.json')['rows']
    metadata, original, all_rows, boundaries = {}, {}, [], {}
    for source in models.SOURCES:
        metadata[source] = load(ref(PREVIOUS / f'inputs/{source}.json'))
        original[source] = arrays(ref(PREVIOUS / f'inputs/{source}.npz'))
        rows = metadata[source]['rows']
        boundaries[source] = slice(len(all_rows), len(all_rows) + len(rows))
        all_rows.extend(rows)
    n = len(all_rows)
    groups = global_groups([*all_rows, *local_rows])
    protected = set(groups[n:])
    retained = np.array([g not in protected for g in groups[:n]])
    cal_x, cal_y, cal_audit = cal_annotations(metadata[models.CAL]['rows'], ref)
    controls = arrays(PREVIOUS / 'primary.predictions.npz')
    directory.mkdir(parents=True)
    (directory / 'inputs').mkdir()
    audit, prepared = {}, {}
    for source in models.SOURCES:
        data, meta = original[source], metadata[source]
        rows, names = meta['rows'], meta['target_names']
        keep = retained[boundaries[source]]
        offsets = data.get('offsets', np.arange(len(rows) + 1))
        chunks, counts = padded(data['embedding'], offsets)
        target = models.reference(source, data['y'], names)
        out = {'x': protocol.mean_chunks(data['embedding'], offsets)[keep],
               'chunks': chunks[keep], 'counts': counts[keep],
               'y': data['y'][keep][:, [names.index(k) for k in models.NATIVE[source]]],
               'reference': target[keep], 'native': data['y'][keep], 'groups': groups[boundaries[source]][keep],
               'ids': np.array([r['id'] for r in rows])[keep],
               'common': data.get('common', np.ones(len(rows), dtype=bool))[keep]}
        if source == models.CAL:
            if not np.allclose(cal_y, target, atol=1e-12, rtol=0):
                raise ValueError('Original exercise votes differ from fixed reference')
            out['semantic'] = cal_x[keep]
        if 'browser' in data:
            out['browser'] = padded(data['browser'], offsets)[0][keep]
        if source in PRIMARY:
            common = np.flatnonzero(data['common'])
            prefix = source + '__energize__'
            for key, value in controls.items():
                if key.startswith(prefix) and key[len(prefix):] not in ('observed', 'outer'):
                    if len(value) != len(common):
                        raise ValueError('Previous control cohort changed')
                    full = np.full(len(rows), np.nan)
                    full[common] = value
                    out['control__' + key[len(prefix):]] = full[keep]
            if not np.allclose(controls[prefix + 'observed'], target[common], atol=1e-12, rtol=0):
                raise ValueError('Fixed external reference changed')
        prepared[source] = out
        save(directory / f'inputs/{source}.json', {**meta, 'rows': [r for r, yes in zip(rows, keep, strict=True) if yes]})
        support = [r['rater_count'] for r, yes in zip(rows, keep, strict=True) if yes and r.get('rater_count') is not None]
        audit[source] = {'supplied_rows': len(rows), 'training_rows': int(keep.sum()),
            'evaluation_rows': int(out['common'].sum()), 'groups': len(set(out['groups'])),
            'excluded_ids': [r['id'] for r, yes in zip(rows, keep, strict=True) if not yes],
            'native_labels': names, 'fitted_labels': models.NATIVE[source], 'chunks': int(out['counts'].sum()),
            'missing_artist_rows': sum(not r.get('artist') for r, yes in zip(rows, keep, strict=True) if yes),
            'annotation_units': dict(Counter(r.get('annotation_unit', 'unknown') for r, yes in zip(rows, keep, strict=True) if yes)),
            'rater_count_min_max': [min(support), max(support)] if support else None,
            'listener_disagreement_rows': sum('listener_std' in r for r, yes in zip(rows, keep, strict=True) if yes)}
    assignments = models.split(prepared, 5)
    for source, data in prepared.items():
        data['outer'] = assignments[source]
        np.savez_compressed(directory / f'inputs/{source}.npz', **data)
    save(directory / 'inputs/audit.json', {'external': audit, 'cal500_original_annotations': cal_audit,
        'local': {'rows': len(local_rows), 'positives': dict(Counter(v for r in local_rows for v in r['labels'])),
                  'new_since_570': sum(not r['prior_inspected'] for r in local_rows),
                  'labels_changed_since_570': sum(r['label_changed_since_570'] for r in local_rows),
                  'skipped': load(local / 'labels.json')['skipped']}})
    paths = code_paths()
    for name, path in paths.items():
        destination = directory / 'source' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    plan = {'created_at': stamp(), 'ticket': 616, 'predecessor': 578, 'runtime': runtime(), 'seed': models.SEED,
        'local': str(local), 'references': references, 'code': {name: digest(p) for name, p in paths.items()},
        'inputs': {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').iterdir()},
        'embedding_signature': previous['embedding_signature'], 'procedures': list(models.METHODS), 'bootstrap_draws': 1000,
        'acceptance': {'macro_ndcg_gain': .02, 'incremental_gain_over_activation': .01,
                       'maximum_domain_ndcg_loss': .02, 'maximum_domain_lift_loss': .01,
                       'regression_controls': ['shared', 'prior_pmemo'], 'positive_paired_domain': True},
        'budgets': {'bytes': 524288, 'warm_three_ms': 100, 'rss_bytes': 1073741824},
        'exposure': 'Previously inspected populations; grouped/omitted-corpus development evidence only.',
        'terms': 'Retain original CAL500/DEAM/PMEmo/Emotify/Soundtracks notices; ManyMusic NC/SA and natural-track terms; Memo CC BY 4.0. Research only; no distribution clearance.'}
    save(directory / 'plan.json', plan)
    validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
                      'rows': audit, 'local_rows': len(local_rows)}), flush=True)


def datasets(directory):
    return {s: arrays(directory / f'inputs/{s}.npz') for s in models.SOURCES}


def fit_stage(directory, stage, training, validation):
    if (directory / (stage + '.receipt.json')).exists():
        check_receipt(directory, stage + '.receipt.json')
        return
    start = time.monotonic()
    print(json.dumps({'stage': stage, 'training_rows': {s: len(d['y']) for s, d in training.items()}}), flush=True)
    model, trace = models.fit(training)
    groups = set(np.concatenate([d['groups'] for d in training.values()]))
    result = {}
    for source, d in validation.items():
        if groups & set(d['groups']):
            raise ValueError('Outer component leaked into training')
        result[source + '__ids'] = d['ids']
        for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
            if key not in d:
                continue
            result.update({source + '__' + prep + '__' + method: v for method, v in models.predict(model, d[key], d['counts']).items()})
            if source in model['experts']:
                result[source + '__' + prep + '__native'] = models.native_average(model['experts'][source]['heads'], d[key], d['counts'])
    path = directory / f'models/{stage}.json'
    compact(path, model)
    save(directory / (stage + '.trace.json'), trace)
    np.savez_compressed(directory / (stage + '.npz'), **result)
    receipt(directory, stage, [path, directory / (stage + '.trace.json'), directory / (stage + '.npz')])
    print(json.dumps({'completed': stage, 'seconds': time.monotonic() - start}), flush=True)


def fit_all(directory):
    data = datasets(directory)
    native = {s: {k: d[k] for k in ('x', 'y', 'chunks', 'counts', 'reference', 'groups', 'ids')} for s, d in data.items()}
    for fold in range(5):
        fit_stage(directory, 'outer-' + str(fold), {s: models.subset(d, data[s]['outer'] != fold) for s, d in native.items()},
                  {s: models.subset(d, d['outer'] == fold) for s, d in data.items()})
    for name, omitted in OMISSIONS.items():
        protected = set(np.concatenate([data[s]['groups'] for s in omitted]))
        training = {s: models.subset(d, np.array([g not in protected for g in d['groups']])) for s, d in native.items() if s not in omitted}
        fit_stage(directory, 'transfer-' + name, training, {s: data[s] for s in omitted})
    fit_stage(directory, 'full', native, {})


def add_controls(d, scores, prep):
    columns = {'shared': 'checkpoint' if prep == 'historical' else 'browser_shared',
               'prior_pmemo': 'pmemo_embedding' if prep == 'historical' else 'pmemo_browser',
               'prior_deam': 'deam_embedding' if prep == 'historical' else 'deam_browser',
               'prior_emotify': 'emotify-artist_embedding' if prep == 'historical' else 'emotify-artist_browser',
               'prior_equal_va': 'equal_va_embedding' if prep == 'historical' else 'equal_va_browser'}
    if prep == 'historical':
        columns.update(refresh_calibration='refresh_calibration', refresh_both='refresh_both', named_prior='named_prior',
                       prior_selected_pmemo='pmemo_prior_selected', prior_selected_emotify='emotify-artist_prior_selected',
                       prior_native='prior_native_mae', prior_activity='activity_native_embedding',
                       source_mean='pmemo_mean', activity_mean='activity_native_training_mean')
    for name, column in columns.items():
        if 'control__' + column in d:
            scores[name] = d['control__' + column]


def metric_report(d, scores, draws):
    pairs = [(m, c) for m in models.METHODS if m in scores
             for c in ('shared', 'prior_pmemo', 'activation', 'direct_activity') if c in scores and c != m]
    # The zero-weight comparisons stay descriptive; they never select an export.
    pairs.extend((m, c) for m, c in [('consensus', 'consensus_no_power'), ('consensus', 'consensus_no_joy'),
                 ('activity', 'activity_activation_only'), ('activity', 'activity_no_power'), ('activity', 'activity_no_joy')]
                 if m in scores and c in scores)
    return {'rows': len(d['reference']), 'groups': len(set(d['groups'])),
            'metrics': {k: {**protocol.metrics(d['reference'], v, d['outer']),
                             'mae': float(np.abs(d['reference'] - v).mean())} for k, v in scores.items()},
            'paired': protocol.paired(d['reference'], scores, d['outer'], d['groups'], pairs, draws) if 'shared' in scores else {}}


def contrasts(source, meta, d, scores):
    names, native = meta['target_names'], d['native']
    get = lambda k: native[:, names.index(k)]
    if source == models.SOUND:
        masks = {'energetic_cheerful': (get('energy') >= .65) & (get('happy') >= .65),
                 'energetic_unhappy': (get('energy') >= .65) & (get('valence') <= .35),
                 'angry_lower_energy_proxy': (get('anger') >= .65) & (get('energy') <= .35)}
    elif source == models.CAL:
        x = d['semantic']
        masks = {'energetic_cheerful': (x[:, 0] >= .65) & (x[:, 5] >= .65),
                 'energetic_low_cheerfulness': (x[:, 0] >= .65) & (x[:, 5] <= .35),
                 'powerful_slower': (x[:, 4] >= .65) & (x[:, 2] <= .35)}
    elif 'arousal' in names and 'valence' in names:
        masks = {'energetic_positive': (get('arousal') >= .65) & (get('valence') >= .65),
                 'energetic_negative': (get('arousal') >= .65) & (get('valence') <= .35)}
    else:
        return {}
    return {name: {'rows': int(mask.sum()), 'ids': d['ids'][mask].tolist(),
                   'reference_mean': float(d['reference'][mask].mean()) if mask.any() else None,
                   'score_means': {k: float(v[mask].mean()) if mask.any() else None for k, v in scores.items()}}
            for name, mask in masks.items()}


def diagnostics(directory, data):
    d = data[models.CAL]
    observed, target = d['semantic'], d['reference']
    scores = {k: np.empty(len(target)) for k in ('observed_attributes', 'predicted_attributes', 'mean')}
    traces = {}
    for fold in range(5):
        train, valid = d['outer'] != fold, d['outer'] == fold
        groups = d['groups'][train]
        inner = models.folds(groups, count=3, seed=models.SEED)
        cross = np.empty_like(observed[train])
        native_traces = {}
        for f in range(3):
            fit, test = inner != f, inner == f
            assignment = models.folds(groups[fit], count=3, seed=models.SEED)
            heads, selected = protocol.select(d['x'][train][fit], observed[train][fit], assignment)
            cross[test] = models.native_average(heads, d['chunks'][train][test], d['counts'][train][test])
            native_traces[str(f)] = {'training_groups': groups[fit].tolist(), 'validation_groups': groups[test].tolist(), **selected}
        heads, selected = protocol.select(d['x'][train], observed[train], inner)
        predicted = models.native_average(heads, d['chunks'][valid], d['counts'][valid])
        for method, xx, vv in (('observed_attributes', observed[train], observed[valid]), ('predicted_attributes', cross, predicted)):
            head = models.map_fit(xx, target[train])
            scores[method][valid] = head_predictions([head], vv)[:, 0]
        scores['mean'][valid] = target[train].mean()
        traces[str(fold)] = {'native_crossfit': native_traces, 'final_native': selected,
                            'training_groups': groups.tolist(), 'validation_groups': d['groups'][valid].tolist()}
    saved = {'observed': target, 'outer': d['outer'], 'groups': d['groups'], **scores}
    np.savez_compressed(directory / 'semantic.npz', **saved)
    save(directory / 'semantic.trace.json', traces)
    common = d['common']
    values = {name: v[common] for name, v in scores.items()}
    return {'inputs': CAL_ATTRIBUTES, 'target': 'Usage-Exercising', 'all_rows': len(target), 'common_rows': int(common.sum()),
            'metrics': {k: protocol.metrics(target[common], v, d['outer'][common]) for k, v in values.items()},
            'paired': protocol.paired(target[common], values, d['outer'][common], d['groups'][common],
                                     [('observed_attributes', 'predicted_attributes'), ('predicted_attributes', 'mean')]),
            'limits': 'Same-rater descriptive diagnostic, raw ordinal encoding; whole-song labels with ten-second audio features.'}


def temporal(directory, data):
    d, meta = data[models.MEMO], load(directory / f'inputs/{models.MEMO}.json')
    output = {}
    for count in (1, 3, 6):
        reference = np.array([np.asarray(r['chunk_targets'])[:count, r['target_names'].index('arousal')].mean() for r in meta['rows']])
        scores = {m: np.empty(len(reference)) for m in models.METHODS}
        for fold in range(5):
            mask = d['outer'] == fold
            prediction = models.predict(load(directory / f'models/outer-{fold}.json'), d['chunks'][mask, :count], np.minimum(d['counts'][mask], count))
            for m in scores:
                scores[m][mask] = prediction[m]
        output[str(count)] = {'rows': len(reference), 'metrics': {m: protocol.metrics(reference, v, d['outer']) for m, v in scores.items()}}
    return output


def local_resources(directory, plan):
    from yt_playlist.core import audio_embedding, sample_files
    path, local = directory / 'models/full.json', Path(plan['local'])
    model, records, all_chunks = load(path), [], []
    for row in load(local / 'labels.json')['rows']:
        samples = [sample_files.read(local / name) for name in row['sample_files']]
        if any(audio_embedding.signature(s) != plan['embedding_signature'] for s in samples):
            raise ValueError('Local embedding identity changed')
        x = np.array([s['embedding']['values'] for s in samples])
        all_chunks.extend(x)
        scores = {stage: {k: float(v[0]) for k, v in models.predict(model, xx[None], np.array([len(xx)])).items()}
                  for stage, xx in (('first', x[:1]), ('three', x[:3]), ('all', x))}
        records.append({'id': row['id'], 'energize_positive': 'energize' in row['labels'], 'samples': len(x), 'scores': scores})
    chunks = np.array(all_chunks)[:, None, :]
    counts = np.ones(len(chunks), dtype=int)
    predicted, independent = models.predict(model, chunks, counts), models.predict(model, chunks, counts, True)
    error = max(float(np.abs(predicted[k] - independent[k]).max()) for k in predicted)
    times = []
    for _ in range(101):
        start = time.perf_counter()
        models.predict(model, chunks[:3, 0][None], np.array([3]))
        times.append(1000 * (time.perf_counter() - start))
    resources = {'bytes': path.stat().st_size, 'warm_three_max_ms': max(times[1:]),
                 'warm_three_median_ms': float(np.median(times[1:])), 'replay_error': error,
                 'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    resources['pass'] = (resources['bytes'] <= plan['budgets']['bytes'] and resources['warm_three_max_ms'] <= plan['budgets']['warm_three_ms']
                         and error <= 1e-12 and resources['peak_rss_bytes'] <= plan['budgets']['rss_bytes'])
    return {'rows': len(records), 'energize_positives': sum(r['energize_positive'] for r in records), 'records': records,
            'limit': 'Supplementary positive-only diagnostics; no accuracy measurement or selection.'}, resources


def decision(report, plan):
    result, rule = {}, plan['acceptance']
    weights = [1., .5, .5, 1., 1.]
    for method in models.METHODS:
        reasons = []
        pairs = [report['outer'][s]['historical']['paired'][method + '__vs__shared'] for s in PRIMARY]
        gain = float(np.average([p['ndcg']['difference'] for p in pairs], weights=weights))
        if gain < rule['macro_ndcg_gain']:
            reasons.append(f'Macro NDCG gain over shared is {gain:.6f}, below 0.02.')
        if not any(p['ndcg']['conditional_95_percent_interval'][0] > 0 for p in pairs):
            reasons.append('No positive paired primary NDCG interval over shared.')
        if method != 'activation':
            incremental = float(np.average([report['outer'][s]['historical']['paired'][method + '__vs__activation']['ndcg']['difference']
                                            for s in PRIMARY], weights=weights))
            if incremental < rule['incremental_gain_over_activation']:
                reasons.append(f'Macro gain over activation is {incremental:.6f}, below 0.01.')
        for regime in ('outer', 'transfer'):
            for source in PRIMARY:
                for prep, table in report[regime][source].items():
                    if method not in table['metrics']:
                        continue
                    for control in rule['regression_controls']:
                        pair = table['paired'][method + '__vs__' + control]
                        if pair['ndcg']['difference'] < -rule['maximum_domain_ndcg_loss'] or pair['top_lift']['difference'] < -rule['maximum_domain_lift_loss']:
                            reasons.append(f'Material regression: {regime}/{source}/{prep} versus {control}.')
        if not report['resources']['pass']:
            reasons.append('Resource/replay bounds failed.')
        result[method] = {'recommendation': 'retain' if reasons else 'candidate_for_review', 'reasons': reasons}
    return result


def evaluate(directory, plan):
    data, saved = datasets(directory), {}
    report = {'created_at': stamp(), 'outer': {}, 'transfer': {}, 'contrasts': {}, 'native': {},
              'transfer_limit': 'No exercise mapping/direct activity fit when CAL500 is omitted; those procedures are absent, not passed.'}
    for source, original in data.items():
        keep, d = original['common'], models.subset(original, original['common'])
        report['outer'][source] = {}
        for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
            if key not in d:
                continue
            print(json.dumps({'evaluate': source, 'preparation': prep}), flush=True)
            scores, native = {}, np.empty_like(original['y'])
            for fold in range(5):
                stage = 'outer-' + str(fold)
                check_receipt(directory, stage + '.receipt.json')
                value, mask = arrays(directory / (stage + '.npz')), original['outer'] == fold
                if not np.array_equal(value[source + '__ids'], original['ids'][mask]):
                    raise ValueError('Outer prediction identity differs')
                prefix = source + '__' + prep + '__'
                for name in value:
                    if name.startswith(prefix) and not name.endswith('__native'):
                        method = name[len(prefix):]
                        scores.setdefault(method, np.empty(len(original['x'])))[mask] = value[name]
                native[mask] = value[prefix + 'native']
            scores = {k: v[keep] for k, v in scores.items()}
            add_controls(d, scores, prep)
            report['outer'][source][prep] = metric_report(d, scores, plan['bootstrap_draws'])
            if prep == 'historical':
                report['contrasts'][source] = contrasts(source, load(directory / f'inputs/{source}.json'), d, scores)
                report['native'][source] = {label: {'mse': float(((native[keep, i] - d['y'][:, i]) ** 2).mean()),
                                                   'mae': float(np.abs(native[keep, i] - d['y'][:, i]).mean())}
                                           for i, label in enumerate(models.NATIVE[source])}
            saved.update({f'outer__{source}__{prep}__{k}': v for k, v in scores.items()})
        saved['ids__' + source] = d['ids']
    for name, omitted in OMISSIONS.items():
        stage = 'transfer-' + name
        check_receipt(directory, stage + '.receipt.json')
        value = arrays(directory / (stage + '.npz'))
        for source in omitted:
            original = data[source]
            keep, d = original['common'], models.subset(original, original['common'])
            if not np.array_equal(value[source + '__ids'], original['ids']):
                raise ValueError('Transfer prediction identity differs')
            report['transfer'][source] = {}
            for prep in ('historical', 'browser'):
                print(json.dumps({'evaluate': stage, 'source': source, 'preparation': prep}), flush=True)
                prefix = source + '__' + prep + '__'
                scores = {k[len(prefix):]: v[keep] for k, v in value.items() if k.startswith(prefix)}
                add_controls(d, scores, prep)
                report['transfer'][source][prep] = metric_report(d, scores, plan['bootstrap_draws'])
                saved.update({f'transfer__{source}__{prep}__{k}': v for k, v in scores.items()})
    print(json.dumps({'stage': 'semantic-diagnostic'}), flush=True)
    report['semantic'] = diagnostics(directory, data)
    report['temporal'] = temporal(directory, data)
    check_receipt(directory, 'full.receipt.json')
    report['local'], report['resources'] = local_resources(directory, plan)
    report['decisions'] = decision(report, plan)
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', **saved)
    receipt(directory, 'evaluation', [directory / name for name in ('report.json', 'predictions.npz', 'semantic.npz', 'semantic.trace.json')])
    print(json.dumps({'decisions': report['decisions'], 'resources': report['resources']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'fit', 'evaluate', 'run', 'verify'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--local', type=Path, default=LOCAL)
    args = parser.parse_args()
    directory, local = inside(args.directory), inside(args.local)
    with threadpool_limits(limits=1):
        if args.command == 'freeze':
            freeze(directory, local)
            return
        plan = validate(directory, args.command != 'verify')
        if args.command in ('fit', 'run'):
            fit_all(directory)
        if args.command in ('evaluate', 'run'):
            evaluate(directory, plan)
        if args.command == 'verify':
            for stage in [*('outer-' + str(f) for f in range(5)), *('transfer-' + s for s in OMISSIONS), 'full', 'evaluation']:
                check_receipt(directory, stage + '.receipt.json')
            print(json.dumps({'verified': str(directory)}), flush=True)


if __name__ == '__main__':
    main()
