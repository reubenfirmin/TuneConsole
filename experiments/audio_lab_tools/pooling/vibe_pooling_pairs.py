"""Joint source calibration from paired measurements of canonical recordings.

A pair contributes one correlated block, never two independent songs. Its
shared track residual separates within-recording source differences from
between-recording variation. This is detached development, not a product FAO.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.sparse import csr_matrix, diags
from scipy.special import expit, logit
from threadpoolctl import threadpool_limits

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.rec import home_vibe, vibe_joint_model as joint
from . import vibe_pooling_evaluation as evaluation, vibe_pooling_metadata as metadata
from . import vibe_pooling_source_assignment as source_assignment

FORMAT = 'paired-source-pooling-development-2'
CODE_FILE = 'experiments/audio_lab_tools/pooling/vibe_pooling_pairs.py'
RECIPE = {**metadata.RECIPE,
    'prior': 'learned full masked heuristic covariate',
    'replicates': 'one block per canonical recording; current audio and recomputed provider, same exact version',
    'residual': 'shared between-track residual plus independent audio/provider measurement residuals',
    'no_applicable_group': 'retain the masked heuristic mean; translate the new-group predictive mixture to that mean',
    'source_population': 'artist-correlated beta-binomial genre/artist source assignment, marginal ML, 96/192-point checked quadrature',
    'gradient': 'forward-relative finite difference, step 1e-5',
    'hypothesis': 'Model source assignment conditional on music metadata instead of assuming library-wide source proportions apply to every genre.',
    'limitations': 'Source families may share classifier bias; plug-in variance uncertainty remains omitted.'}


def identity():
    return {**metadata.identity(), CODE_FILE: evaluation.digest(Path(__file__).read_bytes()),
        'experiments/audio_lab_tools/pooling/vibe_pooling_source_assignment.py': evaluation.digest(
            Path(source_assignment.__file__).read_bytes())}


def observations(rows):
    expanded, owners = [], []
    for index, row in enumerate(rows):
        expanded.append(row)
        owners.append(index)
        if row.get('replicate'):
            replicate = row['replicate']
            if (not row['source'].startswith('audio_measurement/')
                    or not replicate['source'].startswith('provider_measurement/')
                    or not 0 <= replicate['score'] <= 1 or not 0 < replicate['support'] <= 1):
                raise ValueError('Invalid paired source observation')
            expanded.append({**row, **replicate})
            owners.append(index)
    return expanded, np.asarray(owners)


@contextmanager
def design_context():
    with metadata.design_context():
        original_design, original_ordering = joint._design, joint._ordering
        owner = None
        def design(rows):
            nonlocal owner
            expanded, owner = observations(rows)
            return original_design(expanded)
        def ordering(matrix, base_bytes, memory_bytes):
            # Off-diagonal precision inside a pair connects its two source
            # effects. Admit the union of both rows before factorization.
            incidence = csr_matrix((np.ones(len(owner)), (owner, np.arange(len(owner)))),
                                   shape=(int(owner[-1]) + 1, len(owner)))
            graph = incidence @ abs(matrix)
            return original_ordering(graph, base_bytes + matrix.nnz * 64 + len(owner) * 256, memory_bytes)
        joint._design, joint._ordering = design, ordering
        try:
            yield
        finally:
            joint._design, joint._ordering = original_design, original_ordering


def block_precision(owners, track, noise):
    """Inverse and log determinant of track-shared one/two-observation blocks."""
    size = len(owners)
    diagonal = 1 / (track + noise)
    paired = np.flatnonzero(owners[:-1] == owners[1:])
    first, second = track + noise[paired], track + noise[paired + 1]
    determinant = first * second - track * track
    diagonal[paired], diagonal[paired + 1] = second / determinant, first / determinant
    off = -track / determinant
    matrix = diags(diagonal, format='csr') + csr_matrix(
        (np.r_[off, off], (np.r_[paired, paired + 1], np.r_[paired + 1, paired])), shape=(size, size))
    single = np.ones(size, dtype=bool)
    single[paired], single[paired + 1] = False, False
    logdet = np.log(track + noise[single]).sum() + np.log(determinant).sum()
    return matrix, float(logdet)


def fit(rows, vibe):
    rows = joint.unique(rows)
    expanded, owners = observations(rows)
    count = len(rows)
    base_bytes = len(joint.canonical(rows)) * 4 + len(expanded) * 2048 + 8 * 1024 * 1024
    if not rows or count > joint.MAX_ROWS or base_bytes > joint.MEMORY_BYTES:
        raise WorkBudgetExceeded('Paired fit admission failed')
    if any(not row['source'].startswith(('audio_measurement/', 'provider_measurement/'))
           or not 0 <= row['score'] <= 1 or not 0 < row['support'] <= 1
           or not 0 <= row['metadata_prior'] <= 1 for row in expanded):
        raise ValueError('Invalid paired original measurement')
    source_population = source_assignment.fit(rows, base_bytes=base_bytes, memory_bytes=joint.MEMORY_BYTES)
    with threadpool_limits(limits=1):
        design, features, kinds = joint._design(rows)
        order, fill, accounted = joint._ordering(design, base_bytes, joint.MEMORY_BYTES)
        accounted = max(accounted + len(joint.canonical(source_population)) * 4,
                        source_population['accounted_memory_bytes'])
        if accounted > joint.MEMORY_BYTES:
            raise WorkBudgetExceeded('Paired source/posterior exceeds its accounted memory budget')
        design, kinds = design[:, order], [kinds[index] for index in order]
        groups = joint.COMPONENTS[:-1]
        offset = len(groups)
        names = (*groups, 'track', 'residual_provider', 'residual_audio')
        families = np.asarray([offset + 1 + int(row['source'].startswith('audio_measurement/')) for row in expanded])
        active = [index for index, present in enumerate([kind in kinds for kind in groups]
            + [True, bool(np.any(families == offset + 1)), bool(np.any(families == offset + 2))]) if present]
        kind_ids = np.asarray([-1 if kind == 'baseline' else groups.index(kind) for kind in kinds])
        response = np.asarray([row['score'] - .5 for row in expanded])
        provider = families == offset + 1
        supports = np.asarray([math.log(row['support']) for row in expanded])
        center = float(supports[provider].mean()) if np.any(provider) else 0.
        supports = np.where(provider, supports - center, 0.)

        def residuals(variances, slope):
            probability = np.clip((variances[families] - 1e-5) / (.25 - 1e-5), 1e-9, 1 - 1e-9)
            return 1e-5 + (.25 - 1e-5) * expit(logit(probability) + slope * supports)

        def posterior(variances, slope):
            check_stopping()
            prior = np.where(kind_ids < 0, joint.BASELINE_VARIANCE, variances[np.maximum(kind_ids, 0)])
            weights, logdet = block_precision(owners, variances[offset], residuals(variances, slope))
            precision = (design.T @ weights @ design + diags(1 / prior)).tocsc()
            precision.sort_indices()
            factor = joint._factor(precision, fill)
            weighted = weights @ response
            rhs = design.T @ weighted
            mean = factor.solve(rhs)
            loss = .5 * (response @ weighted - rhs @ mean + logdet + np.log(prior).sum()
                + np.log(factor.U.diagonal()).sum() + len(expanded) * np.log(2 * np.pi))
            check_stopping()
            return precision, mean, float(loss)

        def objective(parameters):
            variances = np.zeros(len(names))
            variances[active] = np.exp(parameters[:-1])
            return posterior(variances, parameters[-1])[2]

        initial = [joint.INITIAL[name] if name in groups else .02 for name in names]
        result = minimize(objective, [*np.log([initial[index] for index in active]), 0.],
            method='L-BFGS-B', jac='2-point',
            bounds=[(math.log(1e-5), math.log(.25))] * len(active) + [(-10., 10.)],
            options={'maxiter': 100, 'ftol': 1e-9, 'finite_diff_rel_step': 1e-5})
        if not result.success:
            raise WorkBudgetExceeded('Paired variance fit did not converge: ' + str(result.message))
        variances = np.zeros(len(names))
        variances[active] = np.exp(result.x[:-1])
        slope = float(result.x[-1])
        precision, mean, loss = posterior(variances, slope)
    return {'format': 3, 'vibe': vibe, 'configuration': joint.configuration(), 'inputs': rows,
        'source_assignment': source_population,
        'variances': {**dict(zip(groups, map(float, variances[:offset]))), 'residual': float(variances[offset])},
        'order': order, 'effect_mean': mean.tolist(),
        'precision': {'indptr': joint._encoded(precision.indptr, '<i4'),
            'indices': joint._encoded(precision.indices, '<i4'), 'values': joint._encoded(precision.data, '<f8')},
        'diagnostics': {'development_only': True, 'method': 'block-marginal-maximum-likelihood',
            'iterations': int(result.nit), 'negative_log_likelihood': loss, 'recordings': count,
            'paired_recordings': len(expanded) - count, 'measurement_observations': len(expanded),
            'accounted_memory_bytes': accounted, 'effects': len(features), 'factor_nonzeros': fill,
            'track_residual': float(variances[offset]),
            'source_assignment': {key: value for key, value in source_population.items() if key not in ('artists', 'genres')},
            'genre_audio_prevalence': [{'genre': item['genre'], 'probability': item['audio_mean']}
                for item in source_population['genres']],
            'residual_by_family': dict(zip(names[offset + 1:], map(float, variances[offset + 1:]))),
            'support_slope': slope, 'provider_log_support_center': center,
            'uncertainty': 'conditional-on-fitted-variance-components', 'noise_identified': 'paired-source-assumptions'}}


def predictive_residual(diagnostics, source, support):
    family = 'residual_audio' if source.startswith('audio_measurement/') else 'residual_provider'
    value = diagnostics['residual_by_family'][family]
    probability = np.clip((value - 1e-5) / (.25 - 1e-5), 1e-9, 1 - 1e-9)
    centered = math.log(support) - diagnostics['provider_log_support_center'] if family == 'residual_provider' else 0.
    return float(diagnostics['track_residual'] + 1e-5 + (.25 - 1e-5)
        * expit(logit(probability) + diagnostics['support_slope'] * centered))


def predict(posterior, row):
    unsupported = not posterior.artists.intersection(row['artists']) and row['genre'] not in posterior.genres
    probability = source_assignment.probability(posterior.value['source_assignment'], row['artists'], row['genre'])
    observed = Counter((item['source'], item['support'], item.get('noise', 0.)) for item in posterior.rows)
    totals = Counter()
    for (source, _support, _noise), count in observed.items():
        totals[source.startswith('audio_measurement/')] += count
    population = {key: (probability if key[0].startswith('audio_measurement/') else 1 - probability)
        * count / totals[key[0].startswith('audio_measurement/')] for key, count in observed.items()}
    return metadata.predict(posterior, row, residual_variance=predictive_residual,
        fallback_mean=row['metadata_prior'] if unsupported else None, population=population)


def freeze(source, pairs, destination):
    plan = metadata.frozen_inputs(source)
    captured = json.loads(pairs.read_text())
    if captured['sql_total_changes'] != 0:
        raise ValueError('Paired capture changed its source')
    matched = 0
    for vibe, rows in plan['rows'].items():
        lookup = {row['recording']: row for row in rows}
        for pair in captured['pairs']:
            row, original = lookup.get(pair['recording']), pair['audio'][vibe]
            if (row != original or pair['truncated'] or pair['provider_version'] != original['version']):
                raise ValueError('Paired capture differs from frozen original or has incomplete inputs')
            measured = pair['provider']['fits'].get(vibe)
            if measured:
                row['replicate'] = {**measured, 'source': 'provider_measurement/' + pair['provider_version'],
                    'version': pair['provider_version'], 'input_digest': pair['provider']['input_digest']}
                matched += 1
    priors = {}
    for recording, inputs in plan['profile_inputs'].items():
        profile = evaluation.masked_profile(inputs)
        if profile is None:
            raise ValueError('A hard target has no production profile')
        priors[recording] = {vibe: home_vibe.heuristic_affinity(profile, vibe) for vibe in home_vibe.PRESETS}
    plan.update(format=FORMAT, code=identity(), recipe=RECIPE, metadata_priors=priors,
        pairs_sha256=evaluation.digest(pairs.read_bytes()), hypothesis=RECIPE['hypothesis'])
    plan['limitations'] += ['Independent family measurement residuals and one shared track residual are assumptions, not proof of true suitability.']
    evaluation.save_new(destination, plan)
    return {'plan_sha256': evaluation.digest(destination.read_bytes()), 'recordings': len(plan['assignments']), 'paired_vibe_blocks': matched}


def load(path):
    plan = json.loads(path.read_text())
    if (plan['format'] != FORMAT or plan['code'] != identity() or plan['recipe'] != RECIPE
            or plan['criteria'] != evaluation.CRITERIA or plan['candidate'] != joint.configuration()):
        raise ValueError('Paired-source recipe changed; freeze a separate protocol')
    return plan


def run(path, output, stratum, fold, vibe):
    plan = load(path)
    prepared = [{**row, 'metadata_prior': plan['metadata_priors'][str(row['recording'])][vibe]} for row in plan['rows'][vibe]]
    return metadata.run_prepared(plan, path, output, stratum, fold, vibe, prepared,
        context=design_context, fit_model=fit, predict_model=predict)


def summarize(path, results, output):
    return metadata.summarize_plan(load(path), evaluation.digest(path.read_bytes()), results, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'run', 'summarize'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--pairs', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stratum', choices=('known', 'unseen'))
    parser.add_argument('--fold', type=int, choices=range(5))
    parser.add_argument('--vibe', choices=home_vibe.PRESETS)
    parser.add_argument('--result', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.action == 'freeze':
        if args.source is None or args.pairs is None:
            parser.error('freeze requires --source and --pairs')
        value = freeze(args.source, args.pairs, args.plan)
    elif args.action == 'run':
        if args.output is None or args.stratum is None or args.fold is None or args.vibe is None:
            parser.error('run requires --output, --stratum, --fold and --vibe')
        value = run(args.plan, args.output, args.stratum, args.fold, args.vibe)
    else:
        if args.output is None:
            parser.error('summarize requires --output')
        value = summarize(args.plan, args.result, args.output)
    print(json.dumps(value, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
