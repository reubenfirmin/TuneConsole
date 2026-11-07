"""Freeze and execute #617's Relax consensus on existing verified audio."""
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
from . import relax_consensus as models
from ..support.external_experiment import LAB, digest, save, stamp
from ..focus.focus_experiment import arrays, check_receipt, head_predictions, inside, load, runtime
from ..studies.individual_experiment import compact, receipt
from ..studies.mapped_vibe_rules import apply, labels

PREVIOUS = LAB / 'local/individual-20261030/remaining-1'
DEFAULT = LAB / 'local/relax-617-20261030/round-1'
LOCAL = LAB / 'local/relax-617-20261030/local-1'
EXPERIMENTS = LAB.parent
PRIMARY = (models.SOUND, models.NAT, models.GEN, models.MEMO)
OMISSIONS = {'soundtracks': (models.SOUND,), 'manymusic': (models.NAT, models.GEN), 'memo': (models.MEMO,)}


def code_paths():
    paths = {Path(m.__file__).resolve() for name, m in list(sys.modules.items())
             if (name == 'experiments.audio_lab_tools' or name.startswith('experiments.audio_lab_tools.'))
             and getattr(m, '__file__', None)}
    paths.update((Path(__file__).resolve(), EXPERIMENTS / 'run.py',
                  LAB / 'RELAX_CONSENSUS_PLAN.md', Path(__file__).with_name('test_relax_consensus.py')))
    return {str(p.relative_to(EXPERIMENTS)): p for p in sorted(paths)}


def global_groups(rows):
    # Equal titles are conservative protection, not claims of verified identity.
    decorated = []
    for row in rows:
        titles = sorted(value for kind, value in protocol.keys(row, True) if kind == 'title')
        decorated.append({**row, **({'recording_id': 'title:' + titles[0]} if titles else {})})
    ordinary = protocol.components(rows, include_declared=True)
    # Keep original recording IDs as well as the extra title edges.
    decorated = [{**row, 'group': 'component:' + str(group)} for row, group in zip(decorated, ordinary, strict=True)]
    return protocol.components(decorated, include_declared=True)


def freeze(directory, local):
    from ..studies.individual_experiment import validate as previous_validate
    if directory.exists():
        raise ValueError('Preserve earlier attempts; choose a new directory')
    previous = previous_validate(PREVIOUS)
    check_receipt(PREVIOUS, 'evaluation.receipt.json')
    check_receipt(local, 'receipt.json', False)
    references = {}

    def ref(path):
        path = inside(path)
        references[str(path)] = digest(path)
        return path

    for name in ('plan.json', 'evaluation.receipt.json', 'primary.predictions.npz', 'primary.report.json'):
        ref(PREVIOUS / name)
    ref(local / 'receipt.json')
    for name in load(local / 'receipt.json')['outputs']:
        ref(local / name)
    local_rows = load(local / 'labels.json')['rows']
    meta, original = {}, {}
    all_rows, boundaries = [], {}
    for source in models.SOURCES:
        meta[source] = load(ref(PREVIOUS / f'inputs/{source}.json'))
        original[source] = arrays(ref(PREVIOUS / f'inputs/{source}.npz'))
        rows = meta[source]['rows']
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
        data, metadata = original[source], meta[source]
        rows, names = metadata['rows'], metadata['target_names']
        keep = retained[boundaries[source]]
        offsets = data.get('offsets', np.arange(len(rows) + 1))
        x = protocol.mean_chunks(data['embedding'], offsets)
        reference = apply(models.expression(source), data['y'], names)
        out = {'x': x[keep], 'y': data['y'][keep][:, [names.index(k) for k in models.NATIVE[source]]],
               'reference': reference[keep], 'native': data['y'][keep],
               'groups': groups[boundaries[source]][keep],
               'ids': np.array([r['id'] for r in rows])[keep],
               'common': data.get('common', np.ones(len(rows), dtype=bool))[keep]}
        if 'browser' in data:
            out['browser'] = protocol.mean_chunks(data['browser'], offsets)[keep]
        if source in PRIMARY:
            prior_indices = np.flatnonzero(data['common'])
            for key, value in controls.items():
                prefix = source + '__relax__'
                if key.startswith(prefix) and key[len(prefix):] not in ('observed', 'outer'):
                    if len(value) != len(prior_indices):
                        raise ValueError('Previous control cohort does not match')
                    full = np.full(len(rows), np.nan)
                    full[prior_indices] = value
                    out['control__' + key[len(prefix):]] = full[keep]
            if not np.allclose(controls[source + '__relax__observed'], reference[prior_indices], atol=1e-12):
                raise ValueError('Fixed reference changed')
        prepared[source] = out
        save(directory / f'inputs/{source}.json', {**metadata, 'rows': [r for r, yes in zip(rows, keep, strict=True) if yes]})
        audit[source] = {'supplied_feature_rows': len(rows), 'training_rows': int(keep.sum()),
            'evaluation_rows': int(out['common'].sum()), 'groups': len(set(out['groups'])),
            'local_overlap_excluded_ids': [r['id'] for r, yes in zip(rows, keep, strict=True) if not yes],
            'native_labels': names, 'fitted_labels': models.NATIVE[source],
            'reference': models.expression(source), 'chunks': len(data['embedding']),
            'missing_artist_rows': sum(not r.get('artist') for r in rows),
            'annotation_units': dict(Counter(r['annotation_unit'] for r in rows)),
            'rater_count_min_max': [min(r['rater_count'] for r in rows if r.get('rater_count') is not None),
                                    max(r['rater_count'] for r in rows if r.get('rater_count') is not None)]
                                   if any(r.get('rater_count') is not None for r in rows) else None}
    assignment = models.split(prepared, 5)
    for source, data in prepared.items():
        data['outer'] = assignment[source]
        np.savez_compressed(directory / f'inputs/{source}.npz', **data)
    # Pin full supplied annotation/audio inventories as well as the selected caches.
    inventory = {}
    discovery = LAB / 'local/datasets/discovery'
    for name, relative, audio in (
        ('manymusic', 'manymusic/human-window-index.json', 'manymusic/rated-audio'),
        ('memo', 'memo2496/pilot-human-index.json', 'memo2496/pilot-audio')):
        value = load(ref(discovery / relative))
        inventory[name] = {'indexed_rows': len(value['rows']),
                           'supplied_audio_files': len(list((discovery / audio).glob('*.mp3')))}
    for relative in ('inputs/named-priors.json', 'models/emotify-artist.embedding.json'):
        ref(PREVIOUS / relative)
    save(directory / 'inputs/audit.json', {'created_at': stamp(), 'external': audit, 'inventory': inventory,
        'local': {'usable_rows': len(local_rows), 'positives': dict(Counter(v for r in local_rows for v in r['labels'])),
                  'skipped': load(local / 'labels.json')['skipped'], 'new_since_570': sum(not r['prior_inspected'] for r in local_rows)},
        'prior_combined_pilot_comparison': 'Unavailable: aligned historical cached chunks have audio_attributes=null; no substitute feature policy used.',
        'missing_supervision': 'VA has no calmness/tension co-annotation. Native labels do not establish induced relaxation or product suitability.'})
    paths = code_paths()
    for name, path in paths.items():
        destination = directory / 'source' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    plan = {'created_at': stamp(), 'ticket': 617, 'predecessor': 579, 'runtime': runtime(),
        'seed': models.SEED, 'local': str(local), 'references': references,
        'inputs': {str(p.relative_to(directory)): digest(p) for p in (directory / 'inputs').iterdir()},
        'code': {name: digest(path) for name, path in paths.items()},
        'embedding_signature': previous['embedding_signature'], 'bootstrap_draws': 1000,
        'procedures': ['anchor', 'fixed', 'learned'], 'acceptance': {'macro_ndcg_gain': .02,
            'maximum_domain_ndcg_loss': .02, 'maximum_domain_lift_loss': .01,
            'at_least_one_positive_paired_domain': True, 'transfer_and_browser_no_material_loss': True},
        'budgets': {'bytes': 524288, 'warm_three_ms': 100, 'rss_bytes': 1073741824},
        'exposure': 'All external cohorts previously inspected. New grouped predictions are diagnostic, not fresh confirmation.',
        'terms': 'Retain original source notices: ManyMusic NC/SA and natural-track individual terms, Emotify/Soundtracks source terms, Memo CC BY 4.0; research export only.'}
    save(directory / 'plan.json', plan)
    validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'),
                      'rows': {s: len(d['y']) for s, d in prepared.items()}, 'local_rows': len(local_rows)}), flush=True)


def validate(directory, execution=False):
    plan = load(directory / 'plan.json')
    if execution and runtime() != plan['runtime']:
        raise ValueError('Numerical runtime changed')
    for name, checksum in plan['inputs'].items():
        if digest(directory / name) != checksum:
            raise ValueError('Frozen input changed: ' + name)
    for name, checksum in plan['code'].items():
        if digest(directory / 'source' / name) != checksum:
            raise ValueError('Archived code changed: ' + name)
        if execution and digest(EXPERIMENTS / name) != checksum:
            raise ValueError('Execution code changed; preserve this attempt: ' + name)
    for name, checksum in plan['references'].items():
        if digest(name) != checksum:
            raise ValueError('Referenced artifact changed: ' + name)
    return plan


def datasets(directory):
    return {s: arrays(directory / f'inputs/{s}.npz') for s in models.SOURCES}


def training_data(data):
    return {s: {k: d[k] for k in ('x', 'y', 'reference', 'groups', 'ids')} for s, d in data.items()}


def fit_stage(directory, stage, training, validation):
    if (directory / (stage + '.receipt.json')).exists():
        check_receipt(directory, stage + '.receipt.json')
        return
    print(json.dumps({'stage': stage, 'training_rows': {s: len(d['y']) for s, d in training.items()}}), flush=True)
    model, trace = models.fit(training)
    train_groups = set(np.concatenate([d['groups'] for d in training.values()]))
    result = {}
    for source, d in validation.items():
        if train_groups & set(d['groups']):
            raise ValueError('Outer validation component occurs in training')
        result[source + '__ids'] = d['ids']
        for preparation, key in (('historical', 'x'), ('browser', 'browser')):
            if key in d:
                for method, scores in models.predict(model, d[key]).items():
                    result[source + '__' + preparation + '__' + method] = scores
    model_path = directory / f'models/{stage}.json'
    compact(model_path, model)
    save(directory / (stage + '.trace.json'), trace)
    np.savez_compressed(directory / (stage + '.npz'), **result)
    receipt(directory, stage, [model_path, directory / (stage + '.trace.json'), directory / (stage + '.npz')])


def fit_all(directory):
    data = datasets(directory)
    native = training_data(data)
    for fold in range(5):
        training = {s: models.subset(d, data[s]['outer'] != fold) for s, d in native.items()}
        validation = {s: models.subset(d, d['outer'] == fold) for s, d in data.items()}
        fit_stage(directory, 'outer-' + str(fold), training, validation)
    for name, omitted in OMISSIONS.items():
        protected = set(np.concatenate([data[s]['groups'] for s in omitted]))
        training = {s: models.subset(d, np.array([g not in protected for g in d['groups']]))
                    for s, d in native.items() if s not in omitted}
        fit_stage(directory, 'transfer-' + name, training, {s: data[s] for s in omitted})
    fit_stage(directory, 'full', native, {})


def metric_report(d, scores, draws):
    comparisons = [(candidate, control) for candidate in ('fixed', 'learned')
                   for control in ('anchor', 'prior_embedding') if control in scores]
    return {'rows': len(d['reference']), 'groups': len(set(d['groups'])),
        'metrics': {name: protocol.metrics(d['reference'], value, d['outer']) for name, value in scores.items()},
        'paired': protocol.paired(d['reference'], scores, d['outer'], d['groups'], comparisons, draws)}


def add_controls(d, scores, preparation):
    columns = {'prior_embedding': 'emotify-artist_' + ('embedding' if preparation == 'historical' else 'browser'),
        'shared': 'checkpoint' if preparation == 'historical' else 'browser_shared'}
    if preparation == 'historical':
        columns.update(refresh_calibration='refresh_calibration', refresh_both='refresh_both', prior_combined='named_prior')
    for name, column in columns.items():
        key = 'control__' + column
        if key in d:
            scores[name] = d[key]


def contrast_report(meta, d, scores):
    names, native = meta['target_names'], d['native']
    get = lambda k: native[:, names.index(k)]
    if meta['dataset'] == models.EMO:
        masks = {'quiet_tense': (get('power') <= .35) & (get('tension') >= .65),
                 'calm_sad': (get('calmness') >= .65) & (get('sadness') >= .65)}
    elif meta['dataset'] == models.SOUND:
        masks = {'quiet_tense': (get('energy') <= .35) & (get('tension') >= .65),
                 'low_tension_sad': (get('tension') <= .35) & (get('sad') >= .65)}
    else:
        masks = {'low_arousal_negative_valence_proxy': (get('arousal') <= .35) & (get('valence') <= .35)}
    return {name: {'rows': int(mask.sum()), 'ids': d['ids'][mask].tolist(),
            'reference_mean': float(d['reference'][mask].mean()) if mask.any() else None,
            'score_means': {k: float(v[mask].mean()) if mask.any() else None for k, v in scores.items()}}
            for name, mask in masks.items()}


def diagnostics(directory, data):
    output = {}
    for source, inputs in models.DIAGNOSTIC.items():
        d = data[source]
        meta = load(directory / f'inputs/{source}.json')
        if labels(models.expression(source)) & set(inputs):
            raise ValueError('Semantic diagnostic uses a reference-defining column')
        columns = [meta['target_names'].index(k) for k in inputs]
        observed = d['native'][:, columns]
        predictions = {k: np.empty(len(d['x'])) for k in ('observed_attributes', 'predicted_attributes')}
        for fold in range(5):
            train, valid = d['outer'] != fold, d['outer'] == fold
            groups = d['groups'][train]
            inner = models.folds(groups, count=3, seed=models.SEED)
            cross = np.empty_like(observed[train])
            for f in range(3):
                base_train, base_valid = inner != f, inner == f
                split = models.folds(groups[base_train], count=3, seed=models.SEED)
                heads, _ = protocol.select(d['x'][train][base_train], observed[train][base_train], split)
                cross[base_valid] = head_predictions(heads, d['x'][train][base_valid])
            heads, _ = protocol.select(d['x'][train], observed[train], inner)
            predicted_valid = head_predictions(heads, d['x'][valid])
            for name, xx, vv in (('observed_attributes', observed[train], observed[valid]),
                                 ('predicted_attributes', cross, predicted_valid)):
                heads, _ = protocol.select(xx, d['reference'][train, None], inner)
                predictions[name][valid] = head_predictions(heads, vv)[:, 0]
        output[source] = {'inputs': inputs,
            'metrics': {k: protocol.metrics(d['reference'], v, d['outer']) for k, v in predictions.items()},
            'limit': 'Source-native attribute diagnosis; neither diagnostic changes the fixed reference or selects the consensus.'}
    return output


def local_and_resources(directory, plan):
    model_path = directory / 'models/full.json'
    model = load(model_path)
    local = Path(plan['local'])
    rows = load(local / 'labels.json')['rows']
    records = []
    for row in rows:
        samples = [load(local / name) for name in row['sample_files']]
        x = np.array([s['embedding']['values'] for s in samples])
        if row['embedding_signature'] != plan['embedding_signature']:
            raise ValueError('Local capture embedding signature changed')
        records.append({'id': row['id'], 'relax_positive': 'relax' in row['labels'], 'samples': len(samples),
            'prior_inspected': row['prior_inspected'], 'scores': {stage: {k: float(v[0]) for k, v in
                models.predict(model, xx.mean(axis=0, keepdims=True)).items()}
                for stage, xx in (('first', x[:1]), ('all', x))}})
    x = np.array([r['embedding_values'] for r in rows])
    predicted, independent = models.predict(model, x), models.predict(model, x, True)
    error = max(float(np.max(np.abs(predicted[k] - independent[k]))) for k in predicted)
    elapsed = []
    models.predict(model, x[:3])
    for _ in range(100):
        start = time.perf_counter()
        models.predict(model, x[:3])
        elapsed.append((time.perf_counter() - start) * 1000)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    resources = {'bytes': model_path.stat().st_size, 'warm_three_max_ms': max(elapsed),
                 'warm_three_median_ms': float(np.median(elapsed)), 'replay_error': error, 'peak_rss_bytes': rss}
    resources['pass'] = (resources['bytes'] <= plan['budgets']['bytes'] and max(elapsed) <= 100
                         and error <= 1e-12 and rss <= plan['budgets']['rss_bytes'])
    return {'rows': len(rows), 'relax_positives': sum(r['relax_positive'] for r in records), 'records': records,
            'limit': 'Positive-only local annotations; no false-positive rate, negative labels or selection.'}, resources


def decision(report):
    decisions = {}
    for method in ('fixed', 'learned'):
        reasons = []
        for control in ('anchor', 'prior_embedding'):
            comparisons = [report['outer'][s]['historical']['paired'][method + '__vs__' + control] for s in PRIMARY]
            mean = np.average([p['ndcg']['difference'] for p in comparisons], weights=(1., .5, .5, 1.))
            if mean < .02:
                reasons.append(f'Mean primary NDCG gain over {control} is {mean:.6f}, below 0.02.')
            if not any(p['ndcg']['conditional_95_percent_interval'][0] > 0 for p in comparisons):
                reasons.append('No positive paired primary NDCG interval over ' + control + '.')
        for regime in ('outer', 'transfer'):
            for source, preparations in report[regime].items():
                if source == models.EMO:
                    continue
                for preparation, table in preparations.items():
                    for control in ('anchor', 'prior_embedding'):
                        pair = table['paired'][method + '__vs__' + control]
                        if pair['ndcg']['difference'] < -.02 or pair['top_lift']['difference'] < -.01:
                            reasons.append(f'Material regression: {regime}/{source}/{preparation} versus {control}.')
        if not report['resources']['pass']:
            reasons.append('Resource/replay check failed.')
        decisions[method] = {'recommendation': 'retain' if reasons else 'candidate_for_review', 'reasons': reasons}
    return decisions


def evaluate(directory, plan):
    data = datasets(directory)
    report = {'created_at': stamp(), 'outer': {}, 'transfer': {}, 'contrasts': {}}
    saved = {}
    for source, original in data.items():
        keep = original['common']
        d = models.subset(original, keep)
        report['outer'][source] = {}
        for preparation in ('historical', 'browser'):
            if preparation == 'browser' and 'browser' not in d:
                continue
            scores = {method: np.empty(len(original['x'])) for method in ('anchor', 'fixed', 'learned')}
            for fold in range(5):
                stage = 'outer-' + str(fold)
                check_receipt(directory, stage + '.receipt.json')
                value = arrays(directory / (stage + '.npz'))
                mask = original['outer'] == fold
                if not np.array_equal(value[source + '__ids'], original['ids'][mask]):
                    raise ValueError('Outer prediction identity differs')
                for method in scores:
                    scores[method][mask] = value[source + '__' + preparation + '__' + method]
            scores = {k: v[keep] for k, v in scores.items()}
            add_controls(d, scores, preparation)
            report['outer'][source][preparation] = metric_report(d, scores, plan['bootstrap_draws'])
            if preparation == 'historical':
                report['contrasts'][source] = contrast_report(load(directory / f'inputs/{source}.json'), d, scores)
            saved.update({f'outer__{source}__{preparation}__{k}': v for k, v in scores.items()})
        saved['ids__' + source] = d['ids']
    for name, omitted in OMISSIONS.items():
        stage = 'transfer-' + name
        check_receipt(directory, stage + '.receipt.json')
        value = arrays(directory / (stage + '.npz'))
        for source in omitted:
            original = data[source]
            keep = original['common']
            d = models.subset(original, keep)
            report['transfer'][source] = {}
            if not np.array_equal(value[source + '__ids'], original['ids']):
                raise ValueError('Transfer prediction identity differs')
            for preparation in ('historical', 'browser'):
                scores = {k: value[source + '__' + preparation + '__' + k][keep] for k in ('anchor', 'fixed', 'learned')}
                add_controls(d, scores, preparation)
                report['transfer'][source][preparation] = metric_report(d, scores, plan['bootstrap_draws'])
                saved.update({f'transfer__{source}__{preparation}__{k}': v for k, v in scores.items()})
    print(json.dumps({'stage': 'semantic-diagnostics'}), flush=True)
    report['semantic'] = diagnostics(directory, data)
    check_receipt(directory, 'full.receipt.json')
    report['local'], report['resources'] = local_and_resources(directory, plan)
    report['full_weights'] = {key: load(directory / 'models/full.json')[key]
                              for key in ('expert_names', 'fixed_weights', 'learned_weights')}
    report['decisions'] = decision(report)
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', **saved)
    receipt(directory, 'evaluation', [directory / 'report.json', directory / 'predictions.npz'])
    print(json.dumps({'decisions': report['decisions'], 'weights': report['full_weights'], 'resources': report['resources']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'fit', 'evaluate', 'run', 'validate'))
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    parser.add_argument('--local', type=Path, default=LOCAL)
    args = parser.parse_args()
    directory, local = inside(args.directory), inside(args.local)
    with threadpool_limits(limits=1):
        if args.command == 'freeze':
            freeze(directory, local)
            return
        plan = validate(directory, args.command != 'validate')
        if args.command in ('fit', 'run'):
            fit_all(directory)
        if args.command in ('evaluate', 'run'):
            evaluate(directory, plan)


if __name__ == '__main__':
    main()
