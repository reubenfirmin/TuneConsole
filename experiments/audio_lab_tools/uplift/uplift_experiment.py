"""Freeze, fit and evaluate #618's bounded Uplift consensus comparison."""
import argparse
from collections import Counter
import json
from pathlib import Path
import resource
import shutil
import sys
import time

import numpy as np
from threadpoolctl import threadpool_limits

from ..studies import individual_protocol as protocol
from . import uplift_consensus as models
from ..support.external_experiment import LAB, digest, save, stamp
from ..focus.focus_experiment import arrays, check_receipt, inside, load, runtime
from ..studies.individual_experiment import compact, receipt
from ..studies.mapped_vibe_rules import apply
from ..relax.relax_experiment import global_groups, validate

PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
DEFAULT = LAB / 'local/uplift-618-20261030/round-1'
LOCAL = LAB / 'local/uplift-618-20261030/local-1'
EXPERIMENTS = LAB.parent
PRIMARY = (models.SOUND, models.NAT, models.GEN, models.MEMO)
OMISSIONS = {'soundtracks': (models.SOUND,), 'manymusic': (models.NAT, models.GEN), 'memo': (models.MEMO,)}


def code_paths():
    paths = {Path(m.__file__).resolve() for name, m in list(sys.modules.items())
             if (name == 'experiments.audio_lab_tools' or name.startswith('experiments.audio_lab_tools.'))
             and getattr(m, '__file__', None)}
    paths.update((Path(__file__).resolve(), EXPERIMENTS / 'run.py',
                  Path(__file__).with_name('test_uplift_consensus.py'), Path(__file__).with_name('test_uplift_replay.py')))
    return {str(p.relative_to(EXPERIMENTS)): p for p in sorted(paths)}


def padded(values, offsets):
    counts = np.diff(offsets)
    if np.any(counts < 1) or np.any(counts > 128) or not np.isfinite(values).all():
        raise ValueError('Invalid captured feature intervals')
    out = np.zeros((len(counts), int(max(counts)), values.shape[1]))
    for i, (start, stop) in enumerate(zip(offsets[:-1], offsets[1:], strict=True)):
        out[i, :counts[i]] = values[start:stop]
    return out, counts


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

    for name in ('plan.json', 'evaluation.receipt.json', 'primary.predictions.npz', 'primary.report.json',
                 'inputs/named-priors.json', 'models/deam.embedding.json'):
        ref(PREVIOUS / name)
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
    directory.mkdir(parents=True)
    (directory / 'inputs').mkdir()
    audit, prepared = {}, {}
    controls = arrays(PREVIOUS / 'primary.predictions.npz')
    for source in models.SOURCES:
        data, meta = original[source], metadata[source]
        rows, names = meta['rows'], meta['target_names']
        keep = retained[boundaries[source]]
        offsets = data.get('offsets', np.arange(len(rows) + 1))
        chunks, counts = padded(data['embedding'], offsets)
        target = apply(models.expression(source), data['y'], names)
        out = {'x': protocol.mean_chunks(data['embedding'], offsets)[keep],
               'chunks': chunks[keep], 'counts': counts[keep],
               'y': data['y'][keep][:, [names.index(k) for k in models.NATIVE[source]]],
               'reference': target[keep], 'native': data['y'][keep],
               'groups': groups[boundaries[source]][keep], 'ids': np.array([r['id'] for r in rows])[keep],
               'common': data.get('common', np.ones(len(rows), dtype=bool))[keep]}
        if 'browser' in data:
            out['browser'] = padded(data['browser'], offsets)[0][keep]
        if source in PRIMARY:
            prior_indices = np.flatnonzero(data['common'])
            prefix = source + '__uplift__'
            for key, value in controls.items():
                if key.startswith(prefix) and key[len(prefix):] not in ('observed', 'outer'):
                    if len(value) != len(prior_indices):
                        raise ValueError('Previous control cohort does not match')
                    full = np.full(len(rows), np.nan)
                    full[prior_indices] = value
                    out['control__' + key[len(prefix):]] = full[keep]
            if not np.allclose(controls[prefix + 'observed'], target[prior_indices], atol=1e-12):
                raise ValueError('Fixed reference changed')
        prepared[source] = out
        save(directory / f'inputs/{source}.json', {**meta, 'rows': [r for r, yes in zip(rows, keep, strict=True) if yes]})
        support = [r['rater_count'] for r in rows if r.get('rater_count') is not None]
        audit[source] = {'supplied_feature_rows': len(rows), 'training_rows': int(keep.sum()),
            'evaluation_rows': int(out['common'].sum()), 'groups': len(set(out['groups'])),
            'local_overlap_excluded_ids': [r['id'] for r, yes in zip(rows, keep, strict=True) if not yes],
            'native_labels': names, 'fitted_labels': models.NATIVE[source], 'reference': models.expression(source),
            'chunks': len(data['embedding']), 'missing_artist_rows': sum(not r.get('artist') for r in rows),
            'annotation_units': dict(Counter(r.get('annotation_unit', 'unknown') for r in rows)),
            'rater_count_min_max': [min(support), max(support)] if support else None,
            'listener_disagreement_rows': sum('listener_std' in r for r in rows)}
    assignment = models.split(prepared, 5)
    for source, data in prepared.items():
        data['outer'] = assignment[source]
        np.savez_compressed(directory / f'inputs/{source}.npz', **data)
    save(directory / 'inputs/audit.json', {'created_at': stamp(), 'external': audit,
        'local': {'usable_rows': len(local_rows), 'positives': dict(Counter(v for r in local_rows for v in r['labels'])),
                  'skipped': load(local / 'labels.json')['skipped'], 'new_since_570': sum(not r['prior_inspected'] for r in local_rows)},
        'limits': ['Previously inspected populations; no fresh confirmation.',
                   'Rater disagreement unavailable where source metadata does not retain it.',
                   'VA, happiness, happy votes and joyful activation remain separate native targets.']})
    paths = code_paths()
    for name, path in paths.items():
        destination = directory / 'source' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    plan = {'created_at': stamp(), 'ticket': 618, 'predecessor': 580, 'runtime': runtime(),
        'seed': models.SEED, 'local': str(local), 'references': references,
        'inputs': {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').iterdir()},
        'code': {name: digest(path) for name, path in paths.items()}, 'embedding_signature': previous['embedding_signature'],
        'procedures': list(models.METHODS), 'bootstrap_draws': 1000,
        'acceptance': {'macro_ndcg_gain': .02, 'gain_controls': ['anchor', 'shared'],
            'regression_controls': ['anchor', 'prior_deam', 'shared'],
            'maximum_domain_ndcg_loss': .02, 'maximum_domain_lift_loss': .01,
            'at_least_one_positive_paired_domain': True, 'transfer_and_browser_no_material_loss': True},
        'budgets': {'bytes': 524288, 'warm_three_ms': 100, 'rss_bytes': 1073741824},
        'exposure': 'Inspected populations; grouped/omitted-corpus results are diagnostics, not fresh confirmation.',
        'terms': 'Original DEAM/PMEmo/Emotify/Soundtracks terms, Jamendo noncommercial research terms, ManyMusic NC/SA plus individual natural-track licences, Memo CC BY 4.0; research export only.'}
    save(directory / 'plan.json', plan)
    validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
                      'rows': {s: len(d['y']) for s, d in prepared.items()}, 'local_rows': len(local_rows)}), flush=True)


def datasets(directory):
    return {s: arrays(directory / f'inputs/{s}.npz') for s in models.SOURCES}


def fit_stage(directory, stage, training, validation):
    if (directory / (stage + '.receipt.json')).exists():
        check_receipt(directory, stage + '.receipt.json')
        return
    start = time.monotonic()
    print(json.dumps({'stage': stage, 'training_rows': {s: len(d['y']) for s, d in training.items()}}), flush=True)
    model, trace = models.fit(training)
    train_groups = set(np.concatenate([d['groups'] for d in training.values()]))
    result = {}
    for source, d in validation.items():
        if train_groups & set(d['groups']):
            raise ValueError('Outer validation component occurs in training')
        result[source + '__ids'] = d['ids']
        for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
            if key in d:
                for method, scores in models.predict(model, d[key], d['counts']).items():
                    result[source + '__' + prep + '__' + method] = scores
                if source in model['experts']:
                    result[source + '__' + prep + '__native'] = models.native_average(model['experts'][source]['heads'], d[key], d['counts'])
    path = directory / f'models/{stage}.json'
    compact(path, model)
    save(directory / (stage + '.trace.json'), trace)
    np.savez_compressed(directory / (stage + '.npz'), **result)
    receipt(directory, stage, [path, directory / (stage + '.trace.json'), directory / (stage + '.npz')])
    print(json.dumps({'completed': stage, 'seconds': time.monotonic() - start,
                      'weights': dict(zip(model['expert_names'], model['learned_weights']))}), flush=True)


def fit_all(directory):
    data = datasets(directory)
    native = {s: {k: d[k] for k in ('x', 'y', 'chunks', 'counts', 'reference', 'groups', 'ids')} for s, d in data.items()}
    for fold in range(5):
        fit_stage(directory, 'outer-' + str(fold),
                  {s: models.subset(d, data[s]['outer'] != fold) for s, d in native.items()},
                  {s: models.subset(d, d['outer'] == fold) for s, d in data.items()})
    for name, omitted in OMISSIONS.items():
        protected = set(np.concatenate([data[s]['groups'] for s in omitted]))
        training = {s: models.subset(d, np.array([g not in protected for g in d['groups']]))
                    for s, d in native.items() if s not in omitted}
        fit_stage(directory, 'transfer-' + name, training, {s: data[s] for s in omitted})
    fit_stage(directory, 'full', native, {})


def add_controls(d, scores, prep):
    columns = {'prior_deam': 'deam_' + ('embedding' if prep == 'historical' else 'browser'),
               'shared': 'checkpoint' if prep == 'historical' else 'browser_shared'}
    if prep == 'historical':
        columns.update(refresh_calibration='refresh_calibration', refresh_both='refresh_both', named_prior='named_prior')
    for name, column in columns.items():
        if 'control__' + column in d:
            scores[name] = d['control__' + column]


def metric_report(d, scores, draws):
    pairs = [(m, c) for m in ('fixed', 'learned') for c in ('anchor', 'prior_deam', 'shared', 'named_prior') if c in scores]
    return {'rows': len(d['reference']), 'groups': len(set(d['groups'])),
            'metrics': {k: protocol.metrics(d['reference'], v, d['outer']) for k, v in scores.items()},
            'paired': protocol.paired(d['reference'], scores, d['outer'], d['groups'], pairs, draws) if 'shared' in scores else {}}


def contrast_report(meta, d, scores):
    names, native = meta['target_names'], d['native']
    get = lambda k: native[:, names.index(k)]
    if meta['dataset'] == models.SOUND:
        masks = {'gentle_positive': (get('energy') <= .35) & (get('happy') >= .65),
                 'energetic_negative': (get('energy') >= .65) & (get('valence') <= .35),
                 'positive_forceful': (get('anger') >= .65) & (get('valence') >= .65)}
    elif 'valence' in names and 'arousal' in names:
        masks = {'gentle_positive_proxy': (get('arousal') <= .35) & (get('valence') >= .65),
                 'energetic_negative': (get('arousal') >= .65) & (get('valence') <= .35)}
    else:
        return {}
    return {name: {'rows': int(mask.sum()), 'ids': d['ids'][mask].tolist(),
                   'reference_mean': float(d['reference'][mask].mean()) if mask.any() else None,
                   'score_means': {k: float(v[mask].mean()) if mask.any() else None for k, v in scores.items()}}
            for name, mask in masks.items()}


def diagnostics(directory, data):
    source = models.SOUND
    d, meta = data[source], load(directory / f'inputs/{source}.json')
    names = meta['target_names']
    output, saved = {}, {}
    cases = {'composite_from_other_attributes': (['energy', 'tension', 'anger', 'fear', 'sad', 'tender'], ['happy', 'valence']),
             'happiness_from_valence': (['valence'], ['happy'])}
    for case, (inputs, targets) in cases.items():
        if set(inputs) & set(targets):
            raise ValueError('Diagnostic includes a reference-defining attribute')
        observed = d['native'][:, [names.index(k) for k in inputs]]
        y = d['native'][:, [names.index(k) for k in targets]].mean(axis=1)
        scores = {k: np.empty(len(y)) for k in ('observed_attributes', 'predicted_attributes')}
        for fold in range(5):
            train, valid = d['outer'] != fold, d['outer'] == fold
            groups = d['groups'][train]
            inner = models.folds(groups, count=3, seed=models.SEED)
            cross = np.empty_like(observed[train])
            for f in range(3):
                fit, test = inner != f, inner == f
                split = models.folds(groups[fit], count=3, seed=models.SEED)
                heads, _ = protocol.select(d['x'][train][fit], observed[train][fit], split)
                cross[test] = models.native_average(heads, d['chunks'][train][test], d['counts'][train][test])
            heads, _ = protocol.select(d['x'][train], observed[train], inner)
            predicted = models.native_average(heads, d['chunks'][valid], d['counts'][valid])
            for method, xx, vv in (('observed_attributes', observed[train], observed[valid]),
                                   ('predicted_attributes', cross, predicted)):
                model = protocol.learner.fit_models(xx, y[train, None], [{'family': 'ridge', 'alpha': 100.}])[0]
                scores[method][valid] = protocol.learner.predict(model, vv)[:, 0]
        output[case] = {'inputs': inputs, 'reference_labels': targets,
                       'metrics': {k: protocol.metrics(y, v, d['outer']) for k, v in scores.items()},
                       'limits': 'Diagnostic only; the happiness-only case is not the primary composite reference.'}
        saved.update({case + '__' + k: v for k, v in scores.items()})
        saved[case + '__observed'] = y
    np.savez_compressed(directory / 'semantic.npz', **saved)
    return output


def local_and_resources(directory, plan):
    from yt_playlist.core import audio_embedding, sample_files
    model_path = directory / 'models/full.json'
    model = load(model_path)
    local = Path(plan['local'])
    rows = load(local / 'labels.json')['rows']
    records, all_chunks = [], []
    for row in rows:
        samples = [sample_files.read(local / name) for name in row['sample_files']]
        if any(audio_embedding.signature(s) != plan['embedding_signature'] for s in samples):
            raise ValueError('Local capture embedding signature changed')
        x = np.array([s['embedding']['values'] for s in samples])
        all_chunks.extend(x)
        scores = {stage: {k: float(v[0]) for k, v in models.predict(model, xx[None, :, :], np.array([len(xx)])).items()}
                  for stage, xx in (('first', x[:1]), ('all', x))}
        records.append({'id': row['id'], 'uplift_positive': 'uplift' in row['labels'], 'samples': len(x),
                        'prior_inspected': row['prior_inspected'], 'scores': scores})
    chunks = np.asarray(all_chunks)[:, None, :]
    counts = np.ones(len(chunks), dtype=int)
    predicted, independent = models.predict(model, chunks, counts), models.predict(model, chunks, counts, True)
    error = max(float(np.max(np.abs(predicted[k] - independent[k]))) for k in predicted)
    elapsed, timing_input = [], chunks[:3, 0][None, :, :]
    for _ in range(101):
        start = time.perf_counter()
        models.predict(model, timing_input, np.array([3]))
        elapsed.append((time.perf_counter() - start) * 1000)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    resources = {'bytes': model_path.stat().st_size, 'warm_three_max_ms': max(elapsed[1:]),
                 'warm_three_median_ms': float(np.median(elapsed[1:])), 'replay_error': error, 'peak_rss_bytes': rss}
    resources['pass'] = (resources['bytes'] <= plan['budgets']['bytes'] and max(elapsed[1:]) <= 100
                         and error <= 1e-12 and rss <= plan['budgets']['rss_bytes'])
    return {'rows': len(rows), 'uplift_positives': sum(r['uplift_positive'] for r in records),
            'records': records, 'limit': 'Positive-only supplementary diagnostics; no accuracy or selection.'}, resources


def temporal(directory, data):
    source = models.MEMO
    d, meta = data[source], load(directory / f'inputs/{source}.json')
    output = {}
    for count in (1, 3, 6):
        reference = np.array([np.asarray(r['chunk_targets'])[:count, r['target_names'].index('valence')].mean() for r in meta['rows']])
        scores = {m: np.empty(len(reference)) for m in models.METHODS}
        for fold in range(5):
            mask = d['outer'] == fold
            model = load(directory / f'models/outer-{fold}.json')
            for m, v in models.predict(model, d['chunks'][mask, :count], np.minimum(d['counts'][mask], count)).items():
                scores[m][mask] = v
        output[str(count)] = {'rows': len(reference), 'metrics': {m: protocol.metrics(reference, v, d['outer']) for m, v in scores.items()}}
    return output


def decision(report, plan):
    result, thresholds = {}, plan['acceptance']
    for method in ('fixed', 'learned'):
        reasons = []
        for control in thresholds['gain_controls']:
            pairs = [report['outer'][s]['historical']['paired'][method + '__vs__' + control] for s in PRIMARY]
            gain = float(np.average([p['ndcg']['difference'] for p in pairs], weights=(1., .5, .5, 1.)))
            if gain < thresholds['macro_ndcg_gain']:
                reasons.append(f'Mean NDCG gain over {control} is {gain:.6f}, below 0.02.')
            if not any(p['ndcg']['conditional_95_percent_interval'][0] > 0 for p in pairs):
                reasons.append('No positive primary paired NDCG interval over ' + control + '.')
        for regime in ('outer', 'transfer'):
            for source in PRIMARY:
                for prep, table in report[regime][source].items():
                    for control in thresholds['regression_controls']:
                        pair = table['paired'][method + '__vs__' + control]
                        if pair['ndcg']['difference'] < -.02 or pair['top_lift']['difference'] < -.01:
                            reasons.append(f'Material regression: {regime}/{source}/{prep} versus {control}.')
        if not report['resources']['pass']:
            reasons.append('Resource/replay check failed.')
        result[method] = {'recommendation': 'retain' if reasons else 'candidate_for_review', 'reasons': reasons}
    return result


def evaluate(directory, plan):
    data, saved = datasets(directory), {}
    report = {'created_at': stamp(), 'outer': {}, 'transfer': {}, 'contrasts': {}, 'native': {}}
    for source, original in data.items():
        keep = original['common']
        d = models.subset(original, keep)
        report['outer'][source] = {}
        for prep, key in (('historical', 'chunks'), ('browser', 'browser')):
            if key not in d:
                continue
            print(json.dumps({'evaluate': 'outer', 'source': source, 'preparation': prep}), flush=True)
            scores = {m: np.empty(len(original['x'])) for m in models.METHODS}
            native = np.empty_like(original['y'])
            for fold in range(5):
                stage = 'outer-' + str(fold)
                check_receipt(directory, stage + '.receipt.json')
                value, mask = arrays(directory / (stage + '.npz')), original['outer'] == fold
                if not np.array_equal(value[source + '__ids'], original['ids'][mask]):
                    raise ValueError('Outer prediction identity differs')
                for m in scores:
                    scores[m][mask] = value[source + '__' + prep + '__' + m]
                native[mask] = value[source + '__' + prep + '__native']
            scores = {k: v[keep] for k, v in scores.items()}
            add_controls(d, scores, prep)
            report['outer'][source][prep] = metric_report(d, scores, plan['bootstrap_draws'])
            if prep == 'historical':
                report['contrasts'][source] = contrast_report(load(directory / f'inputs/{source}.json'), d, scores)
                report['native'][source] = {label: {'mse': float(np.mean((native[keep, i] - d['y'][:, i]) ** 2)),
                    'mae': float(np.mean(np.abs(native[keep, i] - d['y'][:, i]))),
                    'anchor_mse': float(np.mean((scores['anchor'] - d['y'][:, i]) ** 2))}
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
            report['transfer'][source] = {}
            if not np.array_equal(value[source + '__ids'], original['ids']):
                raise ValueError('Transfer prediction identity differs')
            for prep in ('historical', 'browser'):
                print(json.dumps({'evaluate': 'transfer', 'source': source, 'preparation': prep}), flush=True)
                scores = {k: value[source + '__' + prep + '__' + k][keep] for k in models.METHODS}
                add_controls(d, scores, prep)
                report['transfer'][source][prep] = metric_report(d, scores, plan['bootstrap_draws'])
                saved.update({f'transfer__{source}__{prep}__{k}': v for k, v in scores.items()})
    print(json.dumps({'stage': 'semantic-diagnostics'}), flush=True)
    report['semantic'] = diagnostics(directory, data)
    report['temporal'] = temporal(directory, data)
    check_receipt(directory, 'full.receipt.json')
    report['local'], report['resources'] = local_and_resources(directory, plan)
    report['full_weights'] = {k: load(directory / 'models/full.json')[k] for k in ('expert_names', 'fixed_weights', 'learned_weights')}
    report['decisions'] = decision(report, plan)
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', **saved)
    receipt(directory, 'evaluation', [directory / 'report.json', directory / 'predictions.npz', directory / 'semantic.npz'])
    print(json.dumps({'decisions': report['decisions'], 'weights': report['full_weights'], 'resources': report['resources']}), flush=True)


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
