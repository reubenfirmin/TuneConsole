"""Detached development of joint pooling with a learned masked-metadata prior.

This is a research representation, never a publishable product model. Provider
and audio residuals are estimated separately; support predicts residual variance
with either sign, rather than being assumed to measure classifier precision.
All human recordings are excluded from both fitting and measurement evaluation.
The supplied label holdout therefore remains unscored by this experiment.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import json
import math
from pathlib import Path
import time

import numpy as np
from scipy.optimize import brentq, minimize
from scipy.sparse import csr_matrix, diags, hstack
from scipy.special import expit, logit, ndtr
from threadpoolctl import threadpool_limits

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping, work_budget
from yt_playlist.rec import home_vibe, vibe_joint_model as joint, vibe_prediction as baseline
from . import vibe_pooling_evaluation as evaluation, vibe_pooling_labels as labels
from . import vibe_pooling_derivatives as derivatives

FORMAT = 'metadata-pooling-development-1'
CODE_FILE = 'experiments/audio_lab_tools/pooling/vibe_pooling_metadata.py'
RECIPE = {'prior': 'learned full masked production heuristic covariate',
    'album': 'nested within canonical artist', 'residual': 'audio/provider separate',
    'support': 'provider centered log support, fitted bounded logit-variance slope of either sign',
    'interval': 'equal-tail empirical source/support Gaussian mixture',
    'mixture_components': 128, 'variance_component_uncertainty': 'not integrated',
    'independent_holdout': False, 'candidate_selected': False}


def identity():
    return {**labels.identity(), CODE_FILE: evaluation.digest(Path(__file__).read_bytes()),
        'experiments/audio_lab_tools/pooling/vibe_pooling_derivatives.py': evaluation.digest(Path(derivatives.__file__).read_bytes())}


def frozen_inputs(source):
    original = labels.load(source)
    humans = {row['recording'] for row in original['human_targets']}
    rows = {vibe: [row for row in values if row['recording'] not in humans]
            for vibe, values in original['rows'].items()}
    ids = {str(row['recording']) for values in rows.values() for row in values}
    plan = {'format': FORMAT, 'source_sha256': evaluation.digest(source.read_bytes()),
        'source': str(source), 'code': identity(), 'recipe': RECIPE, 'criteria': evaluation.CRITERIA,
        'candidate': joint.configuration(), 'human_recordings_excluded': sorted(humans),
        'rows': rows, 'assignments': {key: value for key, value in original['assignments'].items() if key in ids},
        'profile_inputs': {key: value for key, value in original['profile_inputs'].items() if key in ids},
        'profile_mask': evaluation.PROFILE_MASK,
        'hypothesis': 'Learn the legitimate metadata prior jointly with correlated effects; fixed unit offsets can miscalibrate it.',
        'limitations': ['Reused measurement development evidence; not independent qualification.',
            'Human scores and labels are not used, including the new reserved identities.',
            'Measurement holdout does not identify shared classifier bias or true suitability.']}
    return plan


def freeze(source, destination):
    plan = frozen_inputs(source)
    evaluation.save_new(destination, plan)
    return {'sha256': evaluation.digest(destination.read_bytes()), 'recordings': len(plan['assignments']), 'recipe': RECIPE}


def load(path):
    plan = json.loads(path.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity() or plan['recipe'] != RECIPE
            or plan['criteria'] != evaluation.CRITERIA or plan['candidate'] != joint.configuration()):
        raise ValueError('Development configuration changed; freeze a separate plan')
    return plan


@contextmanager
def design_context(*, fixed_offset=False):
    """Scope the experimental design, including the posterior's reconstruction."""
    original_design, original_terms, original_factor = joint._design, joint.terms, joint._factor

    def terms(row, kind):
        if kind == 'album':
            return [(json.dumps([artist, row['album']]), 1 / len(row['artists'])) for artist in row['artists']] \
                if row.get('album') and row['artists'] else []
        return original_terms(row, kind)

    def design(rows):
        matrix, features, kinds = original_design(rows)
        if fixed_offset:
            return matrix, features, kinds
        prior = csr_matrix(np.asarray([row['metadata_prior'] - .5 for row in rows])[:, None])
        return hstack((matrix, prior), format='csr'), [*features, ('baseline', 'metadata-prior')], [*kinds, 'baseline']

    def factor(precision, fill):
        # A nonbinary covariate introduces roundoff asymmetry in the Gram
        # product. Preserve the admitted graph and explicitly symmetrize it.
        symmetric = ((precision + precision.T) * .5).tocsc()
        symmetric.sort_indices()
        precision.data, precision.indices, precision.indptr = symmetric.data, symmetric.indices, symmetric.indptr
        return original_factor(precision, fill)

    joint._design, joint.terms, joint._factor = design, terms, factor
    try:
        yield
    finally:
        joint._design, joint.terms, joint._factor = original_design, original_terms, original_factor


def fit(rows, vibe, *, fixed_offset=False, analytic_gradient=False, finite_gradient='3-point'):
    if finite_gradient not in ('2-point', '3-point'):
        raise ValueError('Unknown finite-difference gradient')
    rows = joint.unique(rows)
    count = len(rows)
    base_bytes = len(joint.canonical(rows)) * 4 + count * 2048 + 8 * 1024 * 1024
    if not rows or count > joint.MAX_ROWS or base_bytes > joint.MEMORY_BYTES:
        raise WorkBudgetExceeded('Development fit admission failed')
    if any(not row['source'].startswith(('audio_measurement/', 'provider_measurement/'))
           or not 0 <= row['score'] <= 1 or not 0 < row['support'] <= 1
           or not 0 <= row['metadata_prior'] <= 1 for row in rows):
        raise ValueError('Invalid development observation')
    with threadpool_limits(limits=1):
        design, features, kinds = joint._design(rows)
        if (('baseline', 'metadata-prior') in features) == fixed_offset:
            raise ValueError('Metadata design and response offset disagree')
        order, fill, accounted = joint._ordering(design, base_bytes, joint.MEMORY_BYTES)
        if analytic_gradient:
            accounted += derivatives.working_bytes(*design.shape)
            if accounted > joint.MEMORY_BYTES:
                raise WorkBudgetExceeded('Marginal derivative workspace exceeds fitting admission')
        design, kinds = design[:, order], [kinds[index] for index in order]
        groups = joint.COMPONENTS[:-1]
        names, offset = (*groups, 'residual_provider', 'residual_audio'), len(groups)
        families = np.asarray([offset + int(row['source'].startswith('audio_measurement/')) for row in rows])
        active = [index for index, present in enumerate(
            [kind in kinds for kind in groups] + [bool(np.any(families == index)) for index in (offset, offset + 1)]) if present]
        kind_ids = np.asarray([-1 if kind == 'baseline' else groups.index(kind) for kind in kinds])
        response = np.asarray([row['score'] - (row['metadata_prior'] if fixed_offset else .5) for row in rows])
        provider = families == offset
        support = np.asarray([math.log(row['support']) for row in rows])
        center = float(support[provider].mean()) if np.any(provider) else 0.
        support = np.where(provider, support - center, 0.)
        noise = np.asarray([row.get('noise', 0.) for row in rows])

        def residuals(variances, slope):
            probability = np.clip((variances[families] - 1e-5) / (.25 - 1e-5), 1e-9, 1 - 1e-9)
            return 1e-5 + (.25 - 1e-5) * expit(logit(probability) + slope * support)

        def posterior(variances, slope, *, gradient=False):
            check_stopping()
            prior = np.where(kind_ids < 0, joint.BASELINE_VARIANCE, variances[np.maximum(kind_ids, 0)])
            diagonal = residuals(variances, slope) + noise
            precision = (design.T @ design.multiply((1 / diagonal)[:, None]) + diags(1 / prior)).tocsc()
            precision.sort_indices()
            factor = joint._factor(precision, fill)
            rhs = design.T @ (response / diagonal)
            mean = factor.solve(rhs)
            loss = .5 * (response @ (response / diagonal) - rhs @ mean + np.log(diagonal).sum()
                + np.log(prior).sum() + np.log(factor.U.diagonal()).sum() + count * np.log(2 * np.pi))
            check_stopping()
            if gradient:
                derivative = derivatives.gradient(design, factor, mean, response, diagonal, kind_ids,
                    variances, families, support, residuals(variances, slope))
                return float(loss), derivative
            return precision, mean, float(loss)

        def objective(parameters):
            variances = np.zeros(len(names))
            variances[active] = np.exp(parameters[:-1])
            if analytic_gradient:
                loss, derivative = posterior(variances, parameters[-1], gradient=True)
                return loss, np.r_[derivative[active], derivative[-1]]
            return posterior(variances, parameters[-1])[2]

        initial = [joint.INITIAL[name] if name in groups else joint.INITIAL['residual'] for name in names]
        parameters = [*np.log([initial[index] for index in active]), 0.]
        result = minimize(objective, parameters, method='L-BFGS-B', jac=True if analytic_gradient else finite_gradient,
            bounds=[(math.log(1e-5), math.log(.25))] * len(active) + [(-10., 10.)],
            options={'maxiter': 100, 'ftol': 1e-9, 'finite_diff_rel_step': 1e-5})
        if not result.success:
            raise WorkBudgetExceeded('Development variance fit did not converge: ' + str(result.message))
        variances = np.zeros(len(names))
        variances[active] = np.exp(result.x[:-1])
        slope = float(result.x[-1])
        precision, mean, loss = posterior(variances, slope)
        residual = float(np.mean(residuals(variances, slope)))
    return {'format': 3, 'vibe': vibe, 'configuration': joint.configuration(), 'inputs': rows,
        'variances': {**dict(zip(groups, map(float, variances[:offset]))), 'residual': residual},
        'order': order, 'effect_mean': mean.tolist(),
        'precision': {'indptr': joint._encoded(precision.indptr, '<i4'),
            'indices': joint._encoded(precision.indices, '<i4'), 'values': joint._encoded(precision.data, '<f8')},
        'diagnostics': {'development_only': True, 'method': 'marginal-maximum-likelihood',
            'fixed_metadata_offset': fixed_offset,
            'gradient': 'exact bounded-column marginal derivative' if analytic_gradient else finite_gradient,
            'iterations': int(result.nit), 'negative_log_likelihood': loss, 'recordings': count,
            'accounted_memory_bytes': accounted, 'effects': len(features), 'factor_nonzeros': fill,
            'residual_by_family': dict(zip(names[offset:], map(float, variances[offset:]))),
            'support_slope': slope, 'provider_log_support_center': center,
            'uncertainty': 'conditional-on-fitted-variance-components', 'noise_identified': False}}


def predict(posterior, row, *, residual_variance=None, fallback_mean=None, population=None,
            measurement_components=('source',)):
    if row['recording'] in posterior.recordings:
        raise ValueError('Target measurement was not hidden')
    vector = np.zeros(len(posterior.features))
    vector[posterior.features['baseline', 'intercept']] = 1.
    fixed_offset = posterior.value['diagnostics'].get('fixed_metadata_offset', False)
    if not fixed_offset:
        vector[posterior.features['baseline', 'metadata-prior']] = row['metadata_prior'] - .5
    for name, value in row.get('covariates', {}).items():
        if not math.isfinite(value):
            raise ValueError('Nonfinite development covariate')
        vector[posterior.features['baseline', name]] = value
    missing = 0.
    for kind in joint.COMPONENTS[:-1]:
        if kind in measurement_components:
            continue
        pairs = joint.terms(row, kind)
        if not pairs:
            missing += posterior.variances[kind]
        for name, weight in pairs:
            index = posterior.features.get((kind, name))
            if index is None:
                missing += posterior.variances[kind] * weight * weight
            else:
                vector[index] = weight
    diagnostics = posterior.value['diagnostics']
    if population is None:
        population = Counter((item['source'], item['support'], item.get('noise', 0.)) for item in posterior.rows)
    if len(population) > RECIPE['mixture_components']:
        raise WorkBudgetExceeded('Development source/support mixture exceeds component bound')
    total = sum(population.values())
    if not total > 0 or any(not math.isfinite(weight) or weight < 0 for weight in population.values()):
        raise ValueError('Invalid predictive source population')
    means, variances, probabilities = [], [], []
    with threadpool_limits(limits=1):
        for (source, support, noise), count in population.items():
            conditional = vector.copy()
            absent = missing
            for kind in measurement_components:
                terms = joint.terms({**row, 'source': source}, kind)
                if not terms:
                    absent += posterior.variances[kind]
                for name, weight in terms:
                    index = posterior.features.get((kind, name))
                    if index is None:
                        if kind != 'source' or len({item['source'] for item in posterior.rows}) > 1:
                            absent += posterior.variances[kind] * weight * weight
                    else:
                        conditional[index] = weight
            family = 'residual_audio' if source.startswith('audio_measurement/') else 'residual_provider'
            residual = diagnostics['residual_by_family'][family]
            probability = np.clip((residual - 1e-5) / (.25 - 1e-5), 1e-9, 1 - 1e-9)
            centered = math.log(support) - diagnostics['provider_log_support_center'] if family == 'residual_provider' else 0.
            residual = (residual_variance(diagnostics, source, support) if residual_variance is not None
                else 1e-5 + (.25 - 1e-5) * expit(logit(probability) + diagnostics['support_slope'] * centered))
            means.append(float((row['metadata_prior'] if fixed_offset else .5) + conditional @ posterior.mean))
            variances.append(max(0., float(absent + conditional @ posterior.factor.solve(conditional))) + residual + noise)
            probabilities.append(count / total)
    means, variances, probabilities = map(np.asarray, (means, variances, probabilities))
    if fallback_mean is not None:
        if not 0 <= fallback_mean <= 1:
            raise ValueError('Invalid unsupported-group fallback mean')
        means = means + fallback_mean - probabilities @ means

    def quantile(probability):
        def residual(value):
            return float(probabilities @ ndtr((value - means) / np.sqrt(variances)) - probability)
        return 0. if residual(0.) >= 0 else 1. if residual(1.) <= 0 else float(brentq(residual, 0., 1., xtol=1e-12))

    return float(np.clip(probabilities @ means, 0., 1.)), [quantile(.05), quantile(.95)]


def run(path, output, stratum, fold, vibe):
    plan = load(path)
    prepared = []
    for row in plan['rows'][vibe]:
        profile = evaluation.masked_profile(plan['profile_inputs'][str(row['recording'])])
        if profile is None:
            raise ValueError('Missing production profile; do not silently drop a target')
        prepared.append({**row, 'metadata_prior': home_vibe.heuristic_affinity(profile, vibe)})
    return run_prepared(plan, path, output, stratum, fold, vibe, prepared)


def run_prepared(plan, path, output, stratum, fold, vibe, prepared, *, context=design_context,
                 fit_model=fit, predict_model=predict):
    started = time.monotonic()
    allocation = plan['assignments']
    hidden = {int(key) for key, value in allocation.items() if value[stratum] == fold}
    training = [row for row in prepared if row['recording'] not in hidden]
    artists = {artist for row in training for artist in row['artists']}
    targets = [row for row in prepared if row['recording'] in hidden and row['artists']
               and (stratum == 'unseen' or artists.intersection(row['artists']))]
    if stratum == 'unseen' and any(artists.intersection(row['artists']) for row in targets):
        raise ValueError('Artist leaked across unseen fold')
    report = {'plan_sha256': evaluation.digest(path.read_bytes()), 'stratum': stratum, 'fold': fold, 'vibe': vibe,
        'training_recordings': len(training), 'target_recordings': len(targets), 'rows': [], 'unscored_recordings': []}
    with context():
        fit_started = time.monotonic()
        try:
            with work_budget(10):
                value = fit_model(training, vibe)
            report['fit_seconds'] = time.monotonic() - fit_started
            posterior = joint.Posterior(value)
        except WorkBudgetExceeded as exc:
            report.update(outcome='deferred', reason=str(exc))
            evaluation.save_new(output, report)
            return report
        stats = {}
        for row in training:
            credit = json.dumps(row['artists'], separators=(',', ':')) if row['artists'] else None
            for group in baseline.groups(credit, row['genre']):
                stats[group] = baseline.adjust(stats.get(group, {}), {'fits': {vibe: row}}, 1)
        for row in targets:
            score, interval = predict_model(posterior, row)
            credit = json.dumps(row['artists'], separators=(',', ':'))
            old = baseline.resolve({vibe: row['metadata_prior']}, None,
                [(key, stats[key]) for key in baseline.groups(credit, row['genre']) if key in stats])['fits'][vibe]['score']
            report['rows'].append({'recording': row['recording'], 'cluster': allocation[str(row['recording'])]['cluster'],
                'source': row['source'], 'truth': row['score'], 'baseline': old, 'candidate': score,
                'interval90': interval, 'human_truth': None})
    report.update(outcome='complete', wall_seconds=time.monotonic() - started, diagnostics=value['diagnostics'])
    evaluation.save_new(output, report)
    return {key: value for key, value in report.items() if key != 'rows'}


def summarize(path, results, output):
    return summarize_plan(load(path), evaluation.digest(path.read_bytes()), results, output)


def summarize_plan(plan, plan_hash, results, output):
    tasks, grouped = {}, defaultdict(list)
    for source in results:
        report = json.loads(source.read_text())
        key = (report['stratum'], report['fold'], report['vibe'])
        if report['plan_sha256'] != plan_hash or key in tasks:
            raise ValueError('Duplicate task or changed development plan')
        tasks[key] = report
        if report['outcome'] == 'complete':
            grouped[key[0], key[2]].extend(report['rows'])
    expected = {(s, f, v) for s in ('known', 'unseen') for f in range(5) for v in home_vibe.PRESETS}
    metrics = {s: {v: evaluation.metrics(grouped[s, v]) for v in home_vibe.PRESETS}
               for s in ('known', 'unseen')}
    failures = []
    for stratum, values in metrics.items():
        for vibe, value in values.items():
            if value is None:
                failures.append([stratum, vibe, 'no measurements'])
                continue
            if value['baseline_ndcg10'] - value['candidate_ndcg10'] > evaluation.CRITERIA['ndcg10_drop']:
                failures.append([stratum, vibe, 'ranking'])
            if not evaluation.CRITERIA['coverage90'][0] <= value['coverage90'] <= evaluation.CRITERIA['coverage90'][1]:
                failures.append([stratum, vibe, 'coverage'])
    complete = set(tasks) == expected and all(row['outcome'] == 'complete' for row in tasks.values())
    result = {'plan_sha256': plan_hash, 'complete': complete, 'development_only': True,
        'candidate_selected': False, 'statistical_qualification': False, 'metrics': metrics,
        'failed_point_gates': failures,
        'source_metrics': {s: {v: {family: evaluation.metrics([row for row in grouped[s, v]
            if row['source'].split('/', 1)[0] == family]) for family in
            sorted({row['source'].split('/', 1)[0] for row in grouped[s, v]})}
            for v in home_vibe.PRESETS} for s in ('known', 'unseen')},
        'limits': plan['limitations'] + ['No bootstrap or operational acceptance is claimed by this development summary.']}
    evaluation.save_new(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run', 'summarize'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if not args.source:
            parser.error('freeze requires --source')
        result = freeze(args.source, args.plan)
    elif args.action == 'run':
        if args.output is None or args.stratum is None or args.fold is None or args.vibe is None:
            parser.error('run requires --output, --stratum, --fold and --vibe')
        result = run(args.plan, args.output, args.stratum, args.fold, args.vibe)
    else:
        if args.output is None:
            parser.error('summarize requires --output')
        result = summarize(args.plan, args.result, args.output)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
