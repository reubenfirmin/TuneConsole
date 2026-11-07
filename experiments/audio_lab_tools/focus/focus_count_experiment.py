"""Freeze, run and verify the bounded #711 count-aware Focus comparison."""
import argparse
import json
from pathlib import Path
import resource
import time

import numpy as np
from threadpoolctl import threadpool_limits

from . import focus_counts as models
from ..studies import individual_protocol as protocol
from ..support import quality_round as evidence
from ..support.external_experiment import DATA, LAB, digest, save, stamp
from .focus_experiment import arrays, check_receipt, head_predictions, inside, load
from .focus_mapping import split
from ..studies.individual_experiment import compact

PREVIOUS = LAB / 'local/focus-615-20261030/round-1'
DEFAULT = LAB / 'local/focus-counts-711-20261101/round-1'
DOCUMENT = LAB / 'FOCUS_COUNTS_PLAN.md'
METHODS = ('binomial', 'beta_binomial', 'ridge', 'training_mean', 'adopted_full')


def freeze(directory):
    check_receipt(PREVIOUS, 'evaluation.receipt.json')
    check_receipt(PREVIOUS, 'full.receipt.json')
    source = PREVIOUS / 'inputs/cal500-raw-activity.npz'
    metadata = PREVIOUS / 'inputs/cal500-raw-activity.json'
    refs = []

    def ref(path):
        refs.append(path)
        return path

    data, meta = arrays(source), load(metadata)
    yes, total, audit = models.reconstruct(meta['rows'], DATA / 'cal500/annotations-source', ref)
    if not np.allclose(yes / total, data['y'], rtol=0, atol=1e-12) or int(data['common'].sum()) != 450:
        raise ValueError('Response reconstruction or common evaluation differs')
    protocol.group_guard(data['groups'], data['outer'])
    # The installed artifact is an exact numerical control, not a held-out fit.
    adopted = LAB.parent.parent / 'src/yt_playlist/rec/data/focus-vibes.json'
    exported = load(PREVIOUS / 'exports/direct.json')
    if load(adopted)['heads'] != exported['heads']:
        raise ValueError('Production heads differ from accepted #615 export')
    inputs = {'cal.npz': source, 'metadata.json': metadata, 'adopted.json': adopted}
    inputs.update({f'prior-outer-{f}.npz': PREVIOUS / f'outer-{f}.npz' for f in range(5)})
    for f in range(5):
        check_receipt(PREVIOUS, f'outer-{f}.receipt.json')
    plan = evidence.freeze(directory, 711, {'document_sha256': digest(DOCUMENT),
        'alphas': models.ALPHAS, 'kappas': models.KAPPAS, 'outer_folds': 5, 'inner_folds': 3,
        'bootstrap_draws': 1000, 'native_rows': len(data['y']), 'common_rows': 450,
        'decision': {'ndcg_gain': .02, 'top_lift_gain': .01, 'native_mse_regression': .01,
                     'adopted_full_ranking_regression': .01},
        'budgets': {'seconds': 7200, 'rss_bytes': 1024 ** 3, 'artifact_bytes': 1024 ** 2}}, inputs,
        references=[*refs, PREVIOUS / 'plan.json', PREVIOUS / 'evaluation.receipt.json'],
        documents=[DOCUMENT, Path(__file__).with_name('test_focus_counts.py')])
    np.savez_compressed(directory / 'inputs/counts.npz', yes=yes, total=total)
    save(directory / 'inputs/count-audit.json', audit)
    for name in ('inputs/counts.npz', 'inputs/count-audit.json'):
        plan['inputs'][name] = digest(directory / name)
    save(directory / 'plan.json', plan)
    evidence.validate(directory, True)
    print(json.dumps({'frozen': str(directory), 'plan_sha256': digest(directory / 'plan.json'), 'audit': audit}), flush=True)


def run(directory):
    plan = evidence.validate(directory, True)
    d, counts = arrays(directory / 'inputs/cal.npz'), arrays(directory / 'inputs/counts.npz')
    started = time.monotonic()
    for fold in range(5):
        stage = f'outer-{fold}'
        if (directory / (stage + '.receipt.json')).exists():
            check_receipt(directory, stage + '.receipt.json')
            continue
        train, valid = d['outer'] != fold, d['outer'] == fold
        groups = d['groups'][train]
        protocol.group_guard(groups, split(groups))
        fitted, traces = {}, {}
        for name in ('binomial', 'beta_binomial'):
            fitted[name], traces[name] = models.select(d['x'][train], counts['yes'][train], counts['total'][train], groups, name)
            print(json.dumps({'fold': fold, 'procedure': name, 'alpha': fitted[name]['selected_alpha'],
                              'kappa': fitted[name]['selected_kappa']}), flush=True)
        ridge, ridge_trace = protocol.select(d['x'][train], d['y'][train], split(groups))
        fitted['ridge'], fitted['training_mean'] = ridge, d['y'][train].mean(0).tolist()
        traces.update(ridge=ridge_trace, training_ids=d['ids'][train].tolist(),
                      training_groups=groups.tolist(), validation_groups=d['groups'][valid].tolist())
        predictions = {'ids': d['ids'][valid]}
        for prep, x in (('historical', d['x'][valid]), ('browser', d['browser'][valid, 0])):
            for name in ('binomial', 'beta_binomial'):
                predictions[prep + '__' + name] = models.predict(fitted[name], x)
            predictions[prep + '__ridge'] = head_predictions(ridge, x, True)
            predictions[prep + '__training_mean'] = np.broadcast_to(fitted['training_mean'], (sum(valid), 2))
            predictions[prep + '__adopted_full'] = head_predictions(load(directory / 'inputs/adopted.json')['heads'], x, True)
            old = arrays(directory / f'inputs/prior-outer-{fold}.npz')
            if not np.array_equal(old['ids'], d['ids'][valid]):
                raise ValueError('Prior row identity differs')
            if not np.allclose(predictions[prep + '__ridge'], old[prep + '__direct'], rtol=0, atol=1e-12):
                raise ValueError('Matched ridge does not replay accepted direct procedure')
        paths = [directory / f'{stage}.model.json', directory / f'{stage}.trace.json', directory / f'{stage}.npz']
        compact(paths[0], fitted)
        compact(paths[1], traces)
        np.savez_compressed(paths[2], **predictions)
        evidence.seal(directory, stage, paths)
        if time.monotonic() - started > plan['protocol']['budgets']['seconds']:
            raise ValueError('Frozen wall-clock budget exceeded')
    evaluate(directory, plan)


def native_interval(losses, groups, outer):
    rng = np.random.default_rng(71101)
    blocks = [[np.flatnonzero((groups == g) & (outer == f)) for g in sorted(set(groups[outer == f]))]
              for f in sorted(set(outer))]
    draws = []
    for _ in range(1000):
        ids = np.concatenate([np.concatenate([b[i] for i in rng.integers(len(b), size=len(b))]) for b in blocks])
        draws.append(losses[ids].mean(axis=0))
    return {'difference': losses.mean(0).tolist(), 'conditional_95_percent_interval': np.quantile(draws, [.025, .975], axis=0).T.tolist()}


def evaluate(directory, plan):
    d, counts = arrays(directory / 'inputs/cal.npz'), arrays(directory / 'inputs/counts.npz')
    pooled, kappas = {}, np.zeros((len(d['y']), 2))
    traces = []
    for fold in range(5):
        check_receipt(directory, f'outer-{fold}.receipt.json')
        v, mask = arrays(directory / f'outer-{fold}.npz'), d['outer'] == fold
        model = load(directory / f'outer-{fold}.model.json')
        kappas[mask] = model['beta_binomial']['selected_kappa']
        if not np.array_equal(v['ids'], d['ids'][mask]):
            raise ValueError('Saved prediction identity differs')
        for key, value in v.items():
            if key != 'ids':
                pooled.setdefault(key, np.empty((len(d['y']), 2)))[mask] = value
        trace = load(directory / f'outer-{fold}.trace.json')
        forbidden = set(d['groups'][mask])
        if forbidden & set(trace['training_groups']):
            raise ValueError('Held-out component in outer training')
        for name in ('binomial', 'beta_binomial'):
            protocol.group_guard(np.asarray(trace[name]['training_groups']), np.asarray(trace[name]['inner_folds']))
            for fit in trace[name]['fits']:
                if (forbidden | set(fit['validation_groups'])) & set(fit['training_groups']):
                    raise ValueError('Component leakage in count selection')
        traces.append(trace)
    common = d['common']
    groups, outer, y = d['groups'][common], d['outer'][common], d['y'][common]
    yes, total = counts['yes'][common], counts['total'][common]
    report = {'created_at': stamp(), 'rows': len(y), 'groups': len(set(groups)),
              'audit': load(directory / 'inputs/count-audit.json'), 'preparations': {},
              'controls': {'adopted_full': 'Exact installed artifact; trained on these songs. Exposed replay only.',
                           'ridge': 'Held-out matched adopted procedure; agrees with accepted #615 predictions within 1e-12.'}}
    saved = {'ids': d['ids'][common], 'groups': groups, 'outer': outer, 'observed': y,
             'yes': yes, 'total': total, **{k: v[common] for k, v in pooled.items()}}
    for prep in ('historical', 'browser'):
        predictions = {m: pooled[prep + '__' + m][common] for m in METHODS}
        scores = {m: p.mean(1) for m, p in predictions.items()}
        pairs = [(m, c) for m in ('binomial', 'beta_binomial') for c in ('ridge', 'training_mean', 'adopted_full')]
        result = {'focus': {m: protocol.metrics(y.mean(1), p, outer) for m, p in scores.items()},
                  'paired_focus': protocol.paired(y.mean(1), scores, outer, groups, pairs, 1000), 'native': {}, 'support_strata': {}}
        for m, p in predictions.items():
            bin_nll = models.nll(yes, total, p)
            beta_nll = models.nll(yes, total, p, kappas[common])
            result['native'][m] = {'mse': ((p - y) ** 2).mean(0).tolist(),
                'mae': np.abs(p - y).mean(0).tolist(), 'mean_binomial_nll': bin_nll.mean(0).tolist(),
                'mean_beta_nll_at_candidate_dispersion': beta_nll.mean(0).tolist(),
                'tasks': {name: protocol.metrics(y[:, col], p[:, col], outer) for col, name in enumerate(('reading', 'studying'))}}
        result['paired_native'] = {m: native_interval(np.column_stack([
            ((predictions[m] - y) ** 2 - (predictions['ridge'] - y) ** 2),
            models.nll(yes, total, predictions[m]) - models.nll(yes, total, predictions['ridge'])]), groups, outer)
            for m in ('binomial', 'beta_binomial')}
        for label, mask in (('two', total.min(1) == 2), ('three', total.min(1) == 3), ('four_plus', total.min(1) >= 4)):
            result['support_strata'][label] = {'songs': int(mask.sum()), 'groups': len(set(groups[mask])),
                'native_mse': {m: ((p[mask] - y[mask]) ** 2).mean(0).tolist() if mask.any() else None for m, p in predictions.items()},
                'mapped': {m: protocol.metrics(y[mask].mean(1), p[mask].mean(1), outer[mask]) if mask.sum() >= 10 else None for m, p in predictions.items()}}
        report['preparations'][prep] = result
    decisions = {}
    for m in ('binomial', 'beta_binomial'):
        checks = []
        for prep, result in report['preparations'].items():
            pair = result['paired_focus'][m + '__vs__ridge']
            exposed = result['paired_focus'][m + '__vs__adopted_full']
            checks.extend([pair['ndcg']['difference'] >= .02, pair['top_lift']['difference'] >= .01,
                pair['ndcg']['conditional_95_percent_interval'][0] > 0,
                max(np.asarray(result['native'][m]['mse']) - result['native']['ridge']['mse']) <= .01,
                exposed['ndcg']['difference'] >= -.01, exposed['top_lift']['difference'] >= -.01])
        decisions[m] = {'recommend': all(checks), 'checks': [bool(c) for c in checks]}
    report['decision'] = {'procedures': decisions, 'conclusion': 'recommend' if any(x['recommend'] for x in decisions.values()) else 'retain',
        'limits': 'Exploratory reused songs; conditional component intervals; unidentified listeners; whole-song labels over a ten-second excerpt. No concentration-benefit claim.'}
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    report['resources'] = {'peak_rss_bytes': peak, 'outer_model_bytes': [p.stat().st_size for p in directory.glob('outer-*.model.json')]}
    if peak > plan['protocol']['budgets']['rss_bytes']:
        raise ValueError('Frozen RSS budget exceeded')
    save(directory / 'report.json', report)
    np.savez_compressed(directory / 'predictions.npz', **saved)
    evidence.seal(directory, 'evaluation', [directory / 'report.json', directory / 'predictions.npz'])
    print(json.dumps({'decision': report['decision'], 'resources': report['resources']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'run', 'verify'])
    parser.add_argument('--directory', type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = inside(args.directory)
    with threadpool_limits(limits=1):
        if args.action == 'freeze':
            freeze(directory)
        elif args.action == 'run':
            run(directory)
        else:
            print(json.dumps(evidence.verify(directory)))


if __name__ == '__main__':
    main()
